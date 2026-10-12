# infra/ipc_bridge.py
"""
IPC Bridge —— Host↔Worker 命名管道双向桥（v1.0 简版）

职责（v5.0 主干）：
    1. 传输：Host 建监听，Worker 连接
    2. 桥接：Worker 事件 → 反序列化 → publish 到 EventBus
             Host 事件 → 序列化 → 推给对应 worker

v1.0 简化项（留空函数，v2.0 填）：
    - ACL：仅当前用户（Windows 命名管道默认已隔离，不加显式 ACL）
    - PID 验证
    - ContractVersion 握手
    - 背压（WorkerAckEvent）

编译期依赖：
    contracts.core / contracts.host / contracts.worker
    （唯一允许同时引用 Host / Worker 契约的模块）

不依赖：
    core 业务逻辑 / event_bus 的具体类型（只通过 IEventBus 接口用）
"""
from __future__ import annotations

import inspect
import json
import logging
import os
import threading
import time
import uuid
from dataclasses import asdict, fields, is_dataclass
from enum import Enum
from multiprocessing.connection import Listener, Client
from typing import Callable, Dict, Optional

from contracts.core import Event, IEventBus
from contracts import core as _c_core
from contracts import host as _c_host
from contracts import worker as _c_worker


logger = logging.getLogger(__name__)


# ==================================================================
#  事件注册表（自动收集 contracts 里所有 Event 子类）
# ==================================================================

def _collect_event_classes() -> Dict[str, type]:
    result: Dict[str, type] = {}
    for module in (_c_core, _c_host, _c_worker):
        for _, obj in inspect.getmembers(module, inspect.isclass):
            if obj is Event or not issubclass(obj, Event):
                continue
            if obj.__module__ != module.__name__:
                continue
            try:
                # 绕过 __init__，只为拿到 EVENT_NAME
                inst = obj.__new__(obj)
                nm = obj.get_event_name(inst)
            except Exception:
                continue
            if nm:
                result[nm] = obj
    return result


_EVENT_CLASSES: Dict[str, type] = _collect_event_classes()


# ==================================================================
#  序列化 / 反序列化
# ==================================================================

def _to_jsonable(obj):
    """递归处理 Enum / dataclass，避免 json.dumps 崩"""
    if isinstance(obj, Enum):
        return obj.value
    if is_dataclass(obj):
        return {k: _to_jsonable(v) for k, v in asdict(obj).items()}
    if isinstance(obj, dict):
        return {k: _to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_jsonable(x) for x in obj]
    return obj


def serialize_event(event: Event) -> bytes:
    """事件 → 一行 JSON bytes（末尾 \\n）"""
    try:
        data = asdict(event)
    except Exception:
        data = {}
    data = _to_jsonable(data)
    obj = {'name': event.get_event_name(), 'data': data}
    line = json.dumps(obj, ensure_ascii=False) + '\n'
    return line.encode('utf-8')


def deserialize_event(line: bytes) -> Optional[Event]:
    """一行 JSON bytes → 事件对象；识别失败返回 None"""
    try:
        obj = json.loads(line.decode('utf-8'))
    except Exception:
        return None

    name = obj.get('name')
    data = obj.get('data') or {}
    cls = _EVENT_CLASSES.get(name)
    if cls is None:
        return None

    try:
        valid = {f.name for f in fields(cls)}
        kwargs = {k: v for k, v in data.items() if k in valid}
        return cls(**kwargs)
    except Exception:
        logger.exception('ipc_bridge: 反序列化失败 name=%s', name)
        return None


# ==================================================================
#  地址规范化
# ==================================================================

_PIPE_PREFIX = r'\\.\pipe\\'


def _normalize_address(addr: str) -> str:
    if os.name == 'nt' and not addr.startswith(_PIPE_PREFIX):
        return _PIPE_PREFIX + addr
    return addr


# ==================================================================
#  Host 侧
# ==================================================================

class IPCServer:
    """
    Host 侧的命名管道监听器。

    用法：
        server = IPCServer(event_bus)
        server.start()
        addr = server.pipe_address   # 传给 Worker
        ...
        server.send_to_worker(worker_id, StartTaskEvent(...))
        server.stop()
    """

    def __init__(self, event_bus: IEventBus,
                 address: Optional[str] = None):
        self._bus = event_bus
        self._raw_address = address or f'yys_ipc_{uuid.uuid4().hex[:8]}'
        self._address = _normalize_address(self._raw_address)

        self._listener: Optional[Listener] = None
        self._accept_thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

        self._conns: Dict[str, object] = {}      # worker_id -> Connection
        self._conns_lock = threading.RLock()

        self._on_worker_connect: Optional[Callable[[str], None]] = None
        self._on_worker_disconnect: Optional[Callable[[str], None]] = None

    # ---------- 生命周期 ----------

    @property
    def pipe_address(self) -> str:
        """给 Worker 用的地址"""
        return self._address

    def start(self) -> None:
        if self._listener is not None:
            return
        self._listener = Listener(self._address)
        self._accept_thread = threading.Thread(
            target=self._accept_loop, name='IPC-Accept', daemon=True)
        self._accept_thread.start()
        logger.info('IPCServer: 已启动 address=%s', self._address)

    def stop(self) -> None:
        self._stop.set()
        with self._conns_lock:
            for conn in self._conns.values():
                try:
                    conn.close()
                except Exception:
                    pass
            self._conns.clear()
        if self._listener is not None:
            try:
                self._listener.close()
            except Exception:
                pass
            self._listener = None
        logger.info('IPCServer: 已停止')

    # ---------- 发送 ----------

    def send_to_worker(self, worker_id: str, event: Event) -> bool:
        with self._conns_lock:
            conn = self._conns.get(worker_id)
        if conn is None:
            return False
        try:
            conn.send_bytes(serialize_event(event))
            return True
        except Exception:
            logger.exception('IPCServer: 发送失败 worker=%s', worker_id)
            self._drop_conn(worker_id)
            return False

    def broadcast(self, event: Event) -> int:
        with self._conns_lock:
            ids = list(self._conns.keys())
        return sum(1 for wid in ids if self.send_to_worker(wid, event))

    def list_workers(self) -> list:
        with self._conns_lock:
            return list(self._conns.keys())

    # ---------- 回调注册 ----------

    def on_worker_connect(self, cb: Callable[[str], None]) -> None:
        self._on_worker_connect = cb

    def on_worker_disconnect(self, cb: Callable[[str], None]) -> None:
        self._on_worker_disconnect = cb

    # ---------- 内部 ----------

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                conn = self._listener.accept()
            except Exception:
                break
            t = threading.Thread(
                target=self._recv_loop, args=(conn,),
                name='IPC-Recv', daemon=True)
            t.start()

    def _recv_loop(self, conn) -> None:
        worker_id: Optional[str] = None
        try:
            while not self._stop.is_set():
                try:
                    line = conn.recv_bytes()
                except (EOFError, OSError):
                    break
                except Exception:
                    logger.exception('IPCServer: recv_bytes 异常')
                    break

                evt = deserialize_event(line)
                if evt is None:
                    continue

                # 首次绑定 worker_id
                if worker_id is None:
                    wid = getattr(evt, 'worker_id', None)
                    if wid:
                        worker_id = wid
                        with self._conns_lock:
                            self._conns[worker_id] = conn
                        logger.info(
                            'IPCServer: worker 已连接 worker_id=%s', worker_id)
                        if self._on_worker_connect:
                            try:
                                self._on_worker_connect(worker_id)
                            except Exception:
                                logger.exception(
                                    'IPCServer: on_worker_connect 回调异常')

                # 桥接到 EventBus
                try:
                    self._bus.publish(evt)
                except Exception:
                    logger.exception('IPCServer: publish 异常')
        finally:
            if worker_id:
                self._drop_conn(worker_id)
                if self._on_worker_disconnect:
                    try:
                        self._on_worker_disconnect(worker_id)
                    except Exception:
                        logger.exception(
                            'IPCServer: on_worker_disconnect 回调异常')
            try:
                conn.close()
            except Exception:
                pass

    def _drop_conn(self, worker_id: str) -> None:
        with self._conns_lock:
            conn = self._conns.pop(worker_id, None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


# ==================================================================
#  Worker 侧
# ==================================================================

class IPCClient:
    """
    Worker 侧的命名管道客户端。

    用法（子进程内）：
        cli = IPCClient(pipe_address)
        if not cli.connect():
            sys.exit(1)
        cli.send(WorkerHelloEvent(worker_id=..., pid=os.getpid(), ...))
        cli.start_recv(on_host_event)   # 接收 Host 命令
        ...
        cli.send(WorkerLogEvent(...))
    """

    def __init__(self, address: str):
        self._address = _normalize_address(address)
        self._conn = None
        self._stop = threading.Event()
        self._send_lock = threading.Lock()
        self._recv_thread: Optional[threading.Thread] = None

    # ---------- 生命周期 ----------

    @property
    def connected(self) -> bool:
        return self._conn is not None and not self._stop.is_set()

    def connect(self, timeout: float = 10.0, retry_interval: float = 0.1) -> bool:
        start = time.time()
        while time.time() - start < timeout:
            if self._stop.is_set():
                return False
            try:
                self._conn = Client(self._address)
                logger.info('IPCClient: 已连接 address=%s', self._address)
                return True
            except Exception:
                time.sleep(retry_interval)
        logger.error('IPCClient: 连接超时 address=%s', self._address)
        return False

    def close(self) -> None:
        self._stop.set()
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

    # ---------- 发送 ----------

    def send(self, event: Event) -> bool:
        if self._conn is None:
            return False
        try:
            with self._send_lock:
                self._conn.send_bytes(serialize_event(event))
            return True
        except Exception:
            logger.exception('IPCClient: 发送失败')
            return False

    # ---------- 接收 ----------

    def start_recv(self, on_event: Callable[[Event], None]) -> None:
        self._recv_thread = threading.Thread(
            target=self._recv_loop, args=(on_event,),
            name='IPC-Recv', daemon=True)
        self._recv_thread.start()

    def _recv_loop(self, on_event: Callable[[Event], None]) -> None:
        while not self._stop.is_set():
            if self._conn is None:
                break
            try:
                line = self._conn.recv_bytes()
            except (EOFError, OSError):
                break
            except Exception:
                logger.exception('IPCClient: recv_bytes 异常')
                break

            evt = deserialize_event(line)
            if evt is None:
                continue
            try:
                on_event(evt)
            except Exception:
                logger.exception('IPCClient: on_event 回调异常')


# ==================================================================
#  自检（可选）
# ==================================================================

def _self_test():
    """最小闭环测试：Host 监听 + Worker 连接 + 双向收发"""
    from infra.event_bus import EventBus
    from contracts.worker import WorkerHelloEvent
    from contracts.host import PauseResourceEvent

    bus = EventBus()
    got = []
    bus.subscribe(WorkerHelloEvent, lambda e: got.append(e))

    server = IPCServer(bus)
    server.start()

    cli = IPCClient(server.pipe_address)
    assert cli.connect(), 'worker 连接失败'

    cli.send(WorkerHelloEvent(worker_id='w1', pid=123, device='test'))
    time.sleep(0.3)
    assert got and got[0].worker_id == 'w1', f'Host 未收到 Hello: {got}'

    cli_msgs = []
    cli.start_recv(lambda e: cli_msgs.append(e))
    time.sleep(0.1)
    server.send_to_worker('w1', PauseResourceEvent(resource_name='continue_button'))
    time.sleep(0.3)
    assert cli_msgs, 'Worker 未收到 PauseResourceEvent'

    cli.close()
    server.stop()
    print('[OK] ipc_bridge self-test passed')


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    _self_test()