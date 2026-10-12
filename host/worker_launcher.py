# host/worker_launcher.py
"""
WorkerLauncher —— 拉起 / 停止 Worker 子进程

改动要点（v4）：
    - 新增 hwnd_parent：worker_entry 里 set_device_hwnd 用的顶层窗口句柄，
      与 hwnd_shot / hwnd_click 区分，避免 fallback 走错句柄。
    - 新增 max_count：UI 上用户填的"次数"，透传到 worker 子进程，
      worker_entry 用它初始化 _TaskTabAdapter.max_count。
    - launch() 失败时清理 _workers 记录，避免后续无法重新启动
    - 移除 StartTaskEvent 发送（任务由 worker_entry 直接运行）

职责：
    - 创建 Worker 进程
    - 建立 IPC 连接（IPCServer 回调通知）
    - 进程退出感知 → 上报 WorkerPermanentlyFailedEvent

依赖：contracts.core / contracts.host / contracts.worker / infra.ipc_bridge
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Dict, Optional

from contracts.core import IEventBus
from contracts.worker import WorkerHealthEvent
from contracts.host import WorkerPermanentlyFailedEvent
from infra.ipc_bridge import IPCServer


logger = logging.getLogger(__name__)


@dataclass
class _WorkerRecord:
    worker_id: str
    device: str
    task_module: str
    task_class: str
    pipe_address: str
    hwnd_parent: int = 0
    hwnd_click: int = 0
    hwnd_shot: int = 0
    shm_name: str = ''
    window_w: int = 0
    window_h: int = 0
    templates_dir: str = ''
    max_count: str = 'inf'
    process: Optional[subprocess.Popen] = None
    connected: bool = False
    last_health_time: float = 0.0
    expected_exit: bool = False


class WorkerLauncher:
    """Worker 进程管理器"""

    def __init__(self, event_bus: IEventBus, ipc_server: IPCServer,
                 base_dir: Optional[str] = None):
        self._bus = event_bus
        self._ipc = ipc_server

        if base_dir is None:
            base_dir = os.path.dirname(
                os.path.dirname(os.path.abspath(__file__)))
        self._base_dir = base_dir

        self._workers: Dict[str, _WorkerRecord] = {}
        self._lock = threading.RLock()

        self._unsub_health = self._bus.subscribe(
            WorkerHealthEvent, self._on_health)
        self._ipc.on_worker_connect(self._on_worker_connect)
        self._ipc.on_worker_disconnect(self._on_worker_disconnect)

    # ==================================================================
    #  启动 / 停止
    # ==================================================================

    def launch(self, worker_id: str, device: str,
               task_module: str, task_class: str,
               hwnd_click: int, hwnd_shot: int,
               shm_name: str = '',
               window_w: int = 0, window_h: int = 0,
               templates_dir: str = '',
               hwnd_parent: int = 0,
               max_count: str = 'inf') -> bool:
        with self._lock:
            if worker_id in self._workers:
                logger.warning('WorkerLauncher: %s 已存在', worker_id)
                return False
            record = _WorkerRecord(
                worker_id=worker_id, device=device,
                task_module=task_module, task_class=task_class,
                pipe_address=self._ipc.pipe_address,
                hwnd_parent=hwnd_parent,
                hwnd_click=hwnd_click, hwnd_shot=hwnd_shot,
                shm_name=shm_name, window_w=window_w, window_h=window_h,
                templates_dir=templates_dir,
                max_count=max_count,
            )
            self._workers[worker_id] = record

        ok = self._spawn(record)
        if not ok:
            with self._lock:
                self._workers.pop(worker_id, None)
            logger.error('WorkerLauncher: 启动失败，已清理记录 %s', worker_id)
        return ok

    def terminate(self, worker_id: str, reason: str = '') -> None:
        # 锁内只做「取记录 + 置标志」，不阻塞
        with self._lock:
            record = self._workers.get(worker_id)
            if record is None:
                return
            record.expected_exit = True
    
        logger.info('WorkerLauncher: 停止 %s reason=%s', worker_id, reason)
    
        # 锁外 kill —— 可能阻塞 2~3 秒
        self._kill_process(record)
    
        # 锁外 kill 完之后再 pop
        with self._lock:
            self._workers.pop(worker_id, None)

    def terminate_all(self) -> None:
        with self._lock:
            ids = list(self._workers.keys())
        for wid in ids:
            self.terminate(wid, reason='shutdown')

    def is_running(self, worker_id: str) -> bool:
        with self._lock:
            record = self._workers.get(worker_id)
        return bool(record and record.process
                    and record.process.poll() is None)

    def list_workers(self) -> list:
        with self._lock:
            return list(self._workers.keys())

    def send_command(self, worker_id: str, event) -> bool:
        return self._ipc.send_to_worker(worker_id, event)

    def shutdown(self) -> None:
        try:
            if self._unsub_health:
                self._unsub_health()
        except Exception:
            pass
        self._unsub_health = None
        self.terminate_all()

    # ==================================================================
    #  内部
    # ==================================================================

    def _spawn(self, record: _WorkerRecord) -> bool:
        entry_script = os.path.join(self._base_dir, 'worker', 'worker_entry.py')
        if not os.path.exists(entry_script):
            logger.error('WorkerLauncher: worker_entry.py 不存在 %s', entry_script)
            return False

        args = [
            sys.executable, entry_script,
            '--pipe', record.pipe_address,
            '--worker-id', record.worker_id,
            '--device', record.device,
            '--task-module', record.task_module,
            '--task-class', record.task_class,
            '--hwnd-parent', str(record.hwnd_parent),
            '--hwnd-click', str(record.hwnd_click),
            '--hwnd-shot', str(record.hwnd_shot),
            '--shm', record.shm_name,
            '--window-w', str(record.window_w),
            '--window-h', str(record.window_h),
            '--templates-dir', record.templates_dir,
            '--max-count', str(record.max_count),
        ]
        try:
            proc = subprocess.Popen(args, cwd=self._base_dir)
        except Exception:
            logger.exception('WorkerLauncher: 启动失败 %s', record.worker_id)
            return False

        record.process = proc
        logger.info('WorkerLauncher: %s 已启动 pid=%s tpl=%s max_count=%s',
                    record.worker_id, proc.pid,
                    record.templates_dir or '(未设置)',
                    record.max_count)

        threading.Thread(
            target=self._watch_process, args=(record,),
            name=f'WL-Watch-{record.worker_id}', daemon=True).start()
        return True

    def _kill_process(self, record: _WorkerRecord) -> None:
        proc = record.process
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.terminate()
            try:
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=1.0)
        except Exception:
            logger.exception('WorkerLauncher: 终止异常 %s', record.worker_id)

    def _watch_process(self, record: _WorkerRecord) -> None:
        proc = record.process
        if proc is None:
            return
        proc.wait()
    
        # 幂等 pop：terminate 可能已经 pop 过
        with self._lock:
            self._workers.pop(record.worker_id, None)
    
        # 退出原因按优先级判定
        if record.expected_exit:
            logger.info('WorkerLauncher: %s 已按预期停止', record.worker_id)
            return
    
        if proc.returncode == 0:
            logger.info('WorkerLauncher: %s 自然完成', record.worker_id)
            return
    
        # 到这里才是真的异常退出
        logger.warning('WorkerLauncher: %s 进程异常退出 code=%s',
                       record.worker_id, proc.returncode)
        try:
            self._bus.publish(WorkerPermanentlyFailedEvent(
                sender='WorkerLauncher',
                worker_id=record.worker_id,
                reason=f'进程退出 code={proc.returncode}',
            ))
        except Exception:
            logger.exception('WorkerLauncher: 发布失败事件异常')

    def _on_worker_connect(self, worker_id: str) -> None:
        with self._lock:
            record = self._workers.get(worker_id)
            if record is None:
                logger.warning('WorkerLauncher: 未知 worker 连接 %s', worker_id)
                return
            record.connected = True
            record.last_health_time = time.time()

        logger.info('WorkerLauncher: %s 已连接', worker_id)

    def _on_worker_disconnect(self, worker_id: str) -> None:
        with self._lock:
            record = self._workers.get(worker_id)
            if record is not None:
                record.connected = False
        logger.info('WorkerLauncher: %s 已断开', worker_id)

    def _on_health(self, evt: WorkerHealthEvent) -> None:
        with self._lock:
            record = self._workers.get(evt.worker_id)
            if record is not None:
                record.last_health_time = time.time()