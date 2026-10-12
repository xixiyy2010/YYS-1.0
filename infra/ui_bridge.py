# infra/ui_bridge.py
"""
UI Bridge —— UI 进程 ↔ 引擎进程 命名管道桥

和 infra/ipc_bridge.py 的区别：
    ipc_bridge   引擎 ↔ 业务子进程    事件流（纯 pub/sub，Event dataclass）
    ui_bridge    UI ↔ 引擎           命令-响应 + 事件推送（纯 dict JSON）

通信格式（每行一条 JSON）：
    {"t": "cmd"|"evt", "n": "名字", "d": {...}}

修复（v2）：
    UIHost 连接竞态：_drop_conn 只关自己持有的 conn。
    旧连接断开时不会误关新连接。

依赖：contracts.ui_protocol
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from multiprocessing.connection import Listener, Client
from typing import Callable, Optional

from contracts.ui_protocol import (
    UI_PIPE_NAME, TYPE_COMMAND, TYPE_EVENT,
)


logger = logging.getLogger(__name__)


# ==================================================================
#  地址规范化
# ==================================================================

_PIPE_PREFIX = r'\\.\pipe\\'


def _normalize_address(addr: str) -> str:
    if os.name == 'nt' and not addr.startswith(_PIPE_PREFIX):
        return _PIPE_PREFIX + addr
    return addr


# ==================================================================
#  消息编解码
# ==================================================================

def _encode(msg_type: str, name: str, data: dict) -> bytes:
    obj = {'t': msg_type, 'n': name, 'd': data or {}}
    line = json.dumps(obj, ensure_ascii=False) + '\n'
    return line.encode('utf-8')


def _decode(raw: bytes) -> Optional[tuple]:
    """返回 (type, name, data) 或 None"""
    try:
        obj = json.loads(raw.decode('utf-8'))
    except Exception:
        return None
    t = obj.get('t')
    n = obj.get('n')
    d = obj.get('d') or {}
    if not t or not n:
        return None
    return t, n, d


# ==================================================================
#  引擎侧
# ==================================================================

class UIHost:
    """
    引擎侧的命名管道监听器（同一时刻只服务一个 UI）。

    用法：
        host = UIHost()
        host.on_command(lambda n, d: print(n, d))
        host.start()
        host.send_event('log', {'message': 'hello'})
        ...
        host.stop()
    """

    def __init__(self, address: str = UI_PIPE_NAME):
        self._raw_address = address
        self._address = _normalize_address(address)

        self._listener: Optional[Listener] = None
        self._accept_thread: Optional[threading.Thread] = None
        self._recv_thread: Optional[threading.Thread] = None
        self._conn = None
        self._conn_lock = threading.RLock()
        self._send_lock = threading.Lock()
        self._stop = threading.Event()

        self._on_command: Optional[Callable[[str, dict], None]] = None
        self._on_connect: Optional[Callable[[], None]] = None
        self._on_disconnect: Optional[Callable[[], None]] = None

    # ---------- 生命周期 ----------

    @property
    def address(self) -> str:
        return self._address

    def start(self) -> bool:
        if self._listener is not None:
            return True
        try:
            self._listener = Listener(self._address)
        except Exception:
            logger.exception('UIHost: 监听失败 %s', self._address)
            self._listener = None
            return False

        self._accept_thread = threading.Thread(
            target=self._accept_loop, name='UIHost-Accept', daemon=True)
        self._accept_thread.start()
        logger.info('UIHost: 已启动 address=%s', self._address)
        return True

    def stop(self) -> None:
        self._stop.set()

        with self._conn_lock:
            conn = self._conn
            self._conn = None
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

        if self._listener is not None:
            try:
                self._listener.close()
            except Exception:
                pass
            self._listener = None

        logger.info('UIHost: 已停止')

    # ---------- 回调注册 ----------

    def on_command(self, cb: Callable[[str, dict], None]) -> None:
        self._on_command = cb

    def on_connect(self, cb: Callable[[], None]) -> None:
        self._on_connect = cb

    def on_disconnect(self, cb: Callable[[], None]) -> None:
        self._on_disconnect = cb

    # ---------- 发送 ----------

    def send_event(self, name: str, data: dict = None) -> bool:
        with self._conn_lock:
            conn = self._conn
        if conn is None:
            return False
        try:
            with self._send_lock:
                conn.send_bytes(_encode(TYPE_EVENT, name, data or {}))
            return True
        except Exception:
            logger.exception('UIHost: send_event 失败 name=%s', name)
            self._drop_conn(conn)
            return False

    @property
    def connected(self) -> bool:
        with self._conn_lock:
            return self._conn is not None

    # ---------- 内部 ----------

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                conn = self._listener.accept()
            except Exception:
                break

            # 只服务一个 UI：新连接顶掉旧的
            with self._conn_lock:
                old = self._conn
                self._conn = conn

            if old is not None:
                try:
                    old.close()
                except Exception:
                    pass

            logger.info('UIHost: UI 已连接')
            if self._on_connect:
                try:
                    self._on_connect()
                except Exception:
                    logger.exception('UIHost: on_connect 回调异常')

            self._recv_thread = threading.Thread(
                target=self._recv_loop, args=(conn,),
                name='UIHost-Recv', daemon=True)
            self._recv_thread.start()

    def _recv_loop(self, conn) -> None:
        try:
            while not self._stop.is_set():
                try:
                    raw = conn.recv_bytes()
                except (EOFError, OSError):
                    break
                except Exception:
                    logger.exception('UIHost: recv_bytes 异常')
                    break

                decoded = _decode(raw)
                if decoded is None:
                    continue
                t, n, d = decoded
                if t != TYPE_COMMAND:
                    continue
                if self._on_command:
                    try:
                        self._on_command(n, d)
                    except Exception:
                        logger.exception(
                            'UIHost: on_command 处理异常 name=%s', n)
        finally:
            self._drop_conn(conn)

    def _drop_conn(self, conn) -> None:
        """
        只关闭传入的 conn。

        仅当它仍是当前连接时才清空 self._conn 并触发 on_disconnect，
        避免旧连接的 finally 误关新连接。
        """
        should_notify = False
        with self._conn_lock:
            if self._conn is conn:
                self._conn = None
                should_notify = True
        try:
            conn.close()
        except Exception:
            pass
        if should_notify:
            logger.info('UIHost: UI 已断开')
            if self._on_disconnect:
                try:
                    self._on_disconnect()
                except Exception:
                    logger.exception('UIHost: on_disconnect 回调异常')


# ==================================================================
#  UI 侧
# ==================================================================

class UIClient:
    """
    UI 侧的命名管道客户端。

    用法：
        cli = UIClient()
        if not cli.connect():
            show_engine_lost()
        cli.start_recv(lambda n, d: print(n, d))
        cli.send_command('sync', {})
        ...
        cli.close()
    """

    def __init__(self, address: str = UI_PIPE_NAME):
        self._address = _normalize_address(address)
        self._conn = None
        self._stop = threading.Event()
        self._send_lock = threading.Lock()
        self._recv_thread: Optional[threading.Thread] = None

    # ---------- 生命周期 ----------

    @property
    def connected(self) -> bool:
        return self._conn is not None and not self._stop.is_set()

    def connect(self, timeout: float = 10.0,
                retry_interval: float = 0.1) -> bool:
        start = time.time()
        while time.time() - start < timeout:
            if self._stop.is_set():
                return False
            try:
                self._conn = Client(self._address)
                logger.info('UIClient: 已连接 %s', self._address)
                return True
            except Exception:
                time.sleep(retry_interval)
        logger.error('UIClient: 连接超时 %s', self._address)
        return False

    def close(self) -> None:
        self._stop.set()
        conn = self._conn
        self._conn = None
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    # ---------- 发送 ----------

    def send_command(self, name: str, data: dict = None) -> bool:
        conn = self._conn
        if conn is None:
            return False
        try:
            with self._send_lock:
                conn.send_bytes(_encode(TYPE_COMMAND, name, data or {}))
            return True
        except Exception:
            logger.exception('UIClient: send_command 失败 name=%s', name)
            return False

    # ---------- 接收 ----------

    def start_recv(self, on_event: Callable[[str, dict], None]) -> None:
        self._recv_thread = threading.Thread(
            target=self._recv_loop, args=(on_event,),
            name='UIClient-Recv', daemon=True)
        self._recv_thread.start()

    def _recv_loop(self, on_event: Callable[[str, dict], None]) -> None:
        try:
            while not self._stop.is_set():
                conn = self._conn
                if conn is None:
                    break
                try:
                    raw = conn.recv_bytes()
                except (EOFError, OSError):
                    break
                except Exception:
                    logger.exception('UIClient: recv_bytes 异常')
                    break

                decoded = _decode(raw)
                if decoded is None:
                    continue
                t, n, d = decoded
                if t != TYPE_EVENT:
                    continue
                try:
                    on_event(n, d)
                except Exception:
                    logger.exception(
                        'UIClient: on_event 处理异常 name=%s', n)
        finally:
            self._stop.set()
            conn = self._conn
            self._conn = None
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass