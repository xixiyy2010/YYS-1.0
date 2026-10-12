# worker/worker_host.py
"""
WorkerHost —— 子进程轻量壳

职责（v5.0）：
    - 实现 IWorkerCommandReceiver / IWorkerResultSender（v1.0 用直调代替接口）
    - 握手：发送 WorkerHelloEvent（含 contract_version）
    - 接收 Host 命令：StopTask / PauseTask / ResumeTask
    - 上报事件：WorkerLogEvent / WorkerProgressEvent / WorkerCompletedEvent 等
    - 心跳：定期发 WorkerHealthEvent
    - 日志降级：IPC 不可用时写本地文件（v1.0 只写不回传）

修复（v2）：
    - StopTask 不再提前关闭 IPC，避免 WorkerCompletedEvent 丢失
    - 移除 StartTaskEvent 处理（任务由 worker_entry 直接运行）

依赖：contracts.core / contracts.worker / infra.ipc_bridge
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Callable, Optional

from contracts.core import Event, ContractVersion
from contracts.worker import (
    WorkerHelloEvent, WorkerLogEvent, WorkerHealthEvent,
    WorkerProgressEvent, WorkerCompletedEvent, WorkerCrashReportEvent,
    StopTaskEvent, PauseTaskEvent, ResumeTaskEvent,
)
from infra.ipc_bridge import IPCClient


logger = logging.getLogger(__name__)


# 心跳间隔（秒）
_HEARTBEAT_INTERVAL = 2.0


class WorkerHost:
    """
    Worker 子进程宿主。

    用法（子进程内）：
        host = WorkerHost(
            pipe_address=sys.argv[1],
            worker_id='w-xxxx',
            device='MuMu安卓设备',
        )
        host.attach_shared_memory(accessor)
        if not host.start():
            sys.exit(1)

        task = CollaborationTask(host)
        host.set_task(task)
        host.run_until_stopped()
    """

    def __init__(self, pipe_address: str, worker_id: str, device: str,
                 fallback_log_path: Optional[str] = None):
        self._pipe_address = pipe_address
        self._worker_id = worker_id
        self._device = device
        self._fallback_log_path = fallback_log_path or os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            '..', 'logs', 'worker_fallback.log')

        self._ipc: Optional[IPCClient] = None
        self._shm_accessor = None          # 由 attach_shared_memory 注入
        self._task = None                   # 由 set_task 注入

        self._stop_event = threading.Event()
        self._heartbeat_thread: Optional[threading.Thread] = None

        # 命令回调（可选）
        self._on_pause: Optional[Callable[[], None]] = None
        self._on_resume: Optional[Callable[[], None]] = None

    # ==================================================================
    #  生命周期
    # ==================================================================

    def attach_shared_memory(self, accessor) -> None:
        """注入 SharedMemoryAccessor（Host 下发共享内存名后由子进程绑定）"""
        self._shm_accessor = accessor

    def set_task(self, task) -> None:
        """注入 IPluginTask 实现"""
        self._task = task

    def set_pause_resume_hooks(self,
                               on_pause: Optional[Callable[[], None]] = None,
                               on_resume: Optional[Callable[[], None]] = None
                               ) -> None:
        """注入 pause/resume 回调（可选）"""
        self._on_pause = on_pause
        self._on_resume = on_resume

    def start(self) -> bool:
        """连接 IPC + 握手 + 启动心跳"""
        self._ipc = IPCClient(self._pipe_address)
        if not self._ipc.connect():
            logger.error('WorkerHost: IPC 连接失败')
            return False
    
        # 握手
        hello = WorkerHelloEvent(
            sender=self._worker_id,
            worker_id=self._worker_id,
            pid=os.getpid(),
            contract_version=ContractVersion.as_str(),
            device=self._device,
        )
        if not self._ipc.send(hello):
            logger.error('WorkerHost: Hello 发送失败')
            return False
        logger.info('WorkerHost: Hello 已发送 worker_id=%s', self._worker_id)
    
        # 启动接收
        self._ipc.start_recv(self._on_host_event)
    
        # 立即发一次初始心跳
        try:
            self.publish(WorkerHealthEvent(
                sender=self._worker_id,
                worker_id=self._worker_id,
                alive=True,
            ))
        except Exception:
            logger.exception('WorkerHost: 初始心跳发送异常')
    
        # 启动心跳循环
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop,
            name='WorkerHost-Heartbeat', daemon=True)
        self._heartbeat_thread.start()
    
        return True

    def stop(self) -> None:
        self._stop_event.set()
        if self._ipc is not None:
            self._ipc.close()
            self._ipc = None

    def run_until_stopped(self) -> None:
        """
        阻塞到任务结束。

        顺序非常重要：
            1. 跑任务
            2. 任务结束 / 抛异常 → 发布 WorkerCompletedEvent
            3. 关 IPC
        这样能保证完成事件一定送达 Host（只要 IPC 还活着）。
        """
        if self._task is None:
            logger.error('WorkerHost: 未注入 task')
            return

        success = True
        message = ''
        try:
            self._task.run()
        except Exception as e:
            import traceback
            success = False
            message = str(e)
            # 崩了也报一次 crash report（保留现场细节）
            self.report_crash(
                crash_reason=f'{e}\n{traceback.format_exc()}')
        finally:
            # 先发完成事件，再关 IPC
            try:
                self.publish(WorkerCompletedEvent(
                    sender=self._worker_id,
                    task_name=getattr(self._task, 'name', ''),
                    success=success,
                    message=message,
                ))
            except Exception:
                logger.exception('WorkerHost: 发布 WorkerCompletedEvent 异常')
            self.stop()

    # ==================================================================
    #  上报接口（业务调用）
    # ==================================================================

    def publish(self, event: Event) -> bool:
        """业务上报事件。IPC 不可用时降级写本地文件。"""
        event.sender = event.sender or self._worker_id
        if self._ipc is None or not self._ipc.connected:
            self._fallback_write(event)
            return False
        ok = self._ipc.send(event)
        if not ok:
            self._fallback_write(event)
        return ok

    def log(self, message: str, level: str = 'info',
            quiet: bool = False) -> None:
        """日志便捷方法。quiet=True → 只落盘，不推 UI。"""
        self.publish(WorkerLogEvent(
            sender=self._worker_id, level=level, message=message,
            quiet=quiet))

    def report_progress(self, task_name: str,
                        completed: int = 0, total: int = 0,
                        message: str = '') -> None:
        self.publish(WorkerProgressEvent(
            sender=self._worker_id,
            task_name=task_name,
            completed=completed,
            total=total,
            message=message,
        ))

    def report_crash(self, crash_reason: str,
                     last_task_id: str = '',
                     in_progress_task_id: str = '') -> None:
        self.publish(WorkerCrashReportEvent(
            sender=self._worker_id,
            last_completed_task_id=last_task_id,
            in_progress_task_id=in_progress_task_id,
            crash_reason=crash_reason,
        ))

    # ==================================================================
    #  接收 Host 命令
    # ==================================================================

    def _on_host_event(self, event: Event) -> None:
        if isinstance(event, StopTaskEvent):
            self._handle_stop_task(event)
        elif isinstance(event, PauseTaskEvent):
            self._handle_pause_task(event)
        elif isinstance(event, ResumeTaskEvent):
            self._handle_resume_task(event)
        # StartTaskEvent 已废弃：任务由 worker_entry 在 set_task 后直接运行

    def _handle_stop_task(self, evt: StopTaskEvent) -> None:
        logger.info('WorkerHost: 收到 StopTaskEvent reason=%s', evt.reason)
        self._stop_event.set()
        # 关键修复：只通知 task 停止，不直接关 IPC。
        # IPC 关闭由 run_until_stopped 的 finally 在发完完成事件后再做。
        if self._task is not None:
            try:
                self._task.stop()
            except Exception:
                logger.exception('WorkerHost: task.stop() 异常')

    def _handle_pause_task(self, evt: PauseTaskEvent) -> None:
        logger.info('WorkerHost: 收到 PauseTaskEvent')
        if self._on_pause:
            try:
                self._on_pause()
            except Exception:
                logger.exception('WorkerHost: on_pause 异常')

    def _handle_resume_task(self, evt: ResumeTaskEvent) -> None:
        logger.info('WorkerHost: 收到 ResumeTaskEvent')
        if self._on_resume:
            try:
                self._on_resume()
            except Exception:
                logger.exception('WorkerHost: on_resume 异常')

    # ==================================================================
    #  心跳
    # ==================================================================

    def _heartbeat_loop(self) -> None:
        while not self._stop_event.is_set():
            time.sleep(_HEARTBEAT_INTERVAL)
            if self._stop_event.is_set():
                break
            try:
                self.publish(WorkerHealthEvent(
                    sender=self._worker_id,
                    worker_id=self._worker_id,
                    alive=True,
                ))
            except Exception:
                logger.exception('WorkerHost: 心跳发送异常')

    # ==================================================================
    #  日志降级
    # ==================================================================

    def _fallback_write(self, event: Event) -> None:
        """IPC 不可用时，把事件写本地文件（v1.0：只写不回传）"""
        try:
            os.makedirs(os.path.dirname(self._fallback_log_path), exist_ok=True)
            line = f'{time.strftime("%Y-%m-%d %H:%M:%S")} ' \
                   f'[{event.get_event_name()}] {event}\n'
            with open(self._fallback_log_path, 'a', encoding='utf-8') as f:
                f.write(line)
        except Exception:
            pass

    # ==================================================================
    #  属性
    # ==================================================================

    @property
    def worker_id(self) -> str:
        return self._worker_id

    @property
    def device(self) -> str:
        return self._device

    @property
    def shm_accessor(self):
        return self._shm_accessor

    @property
    def stopped(self) -> bool:
        return self._stop_event.is_set()