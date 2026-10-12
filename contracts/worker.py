# contracts/worker.py
"""
Contracts.Worker

Worker 侧事件名与专属 DTO：

    - 启动命令（Host → Worker）
    - 进度 / 完成回报（Worker → Host）
    - 握手事件（Hello）
    - 心跳 / 日志 / 背压 / 崩溃报告

编译期：Worker 侧代码只允许 import 这个文件 + contracts.core

依赖：contracts.core
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from .core import Event, SCHEMA_VERSION


# ==================================================================
#  Worker 事件名
# ==================================================================

class WorkerEventNames:
    # ---- Host → Worker 命令 ----
    CMD_START_TASK = 'cmd_start_task'
    CMD_STOP_TASK = 'cmd_stop_task'
    CMD_PAUSE_TASK = 'cmd_pause_task'
    CMD_RESUME_TASK = 'cmd_resume_task'

    # ---- Worker → Host 回报 ----
    WORKER_HELLO = 'worker_hello'
    WORKER_LOG = 'worker_log'
    WORKER_HEALTH = 'worker_health'
    WORKER_PROGRESS = 'worker_progress'
    WORKER_COMPLETED = 'worker_completed'
    WORKER_ACK = 'worker_ack'
    WORKER_CRASH_REPORT = 'worker_crash_report'
    WORKER_BACKPRESSURE = 'worker_backpressure'
    WORKER_PERMANENTLY_FAILED = 'worker_permanently_failed'


# ==================================================================
#  Worker 专属 DTO
# ==================================================================

# ---- Host → Worker ----

@dataclass
class StartTaskEvent(Event):
    schema_version: str = SCHEMA_VERSION
    task_name: str = ''
    params: Dict[str, Any] = field(default_factory=dict)

    def get_event_name(self) -> str:
        return WorkerEventNames.CMD_START_TASK


@dataclass
class StopTaskEvent(Event):
    reason: str = ''

    def get_event_name(self) -> str:
        return WorkerEventNames.CMD_STOP_TASK


@dataclass
class PauseTaskEvent(Event):
    def get_event_name(self) -> str:
        return WorkerEventNames.CMD_PAUSE_TASK


@dataclass
class ResumeTaskEvent(Event):
    def get_event_name(self) -> str:
        return WorkerEventNames.CMD_RESUME_TASK


# ---- Worker → Host ----

@dataclass
class WorkerHelloEvent(Event):
    schema_version: str = SCHEMA_VERSION
    worker_id: str = ''
    pid: int = 0
    contract_version: str = ''
    device: str = ''

    def get_event_name(self) -> str:
        return WorkerEventNames.WORKER_HELLO


@dataclass
class WorkerLogEvent(Event):
    schema_version: str = SCHEMA_VERSION
    level: str = 'info'
    message: str = ''
    quiet: bool = False          # True → 只落盘，不推 UI（主 UI / 悬浮窗都不播）

    def get_event_name(self) -> str:
        return WorkerEventNames.WORKER_LOG


@dataclass
class WorkerHealthEvent(Event):
    schema_version: str = SCHEMA_VERSION
    worker_id: str = ''
    alive: bool = True

    def get_event_name(self) -> str:
        return WorkerEventNames.WORKER_HEALTH


@dataclass
class WorkerProgressEvent(Event):
    schema_version: str = SCHEMA_VERSION
    task_name: str = ''
    completed: int = 0
    total: int = 0
    message: str = ''

    def get_event_name(self) -> str:
        return WorkerEventNames.WORKER_PROGRESS


@dataclass
class WorkerCompletedEvent(Event):
    schema_version: str = SCHEMA_VERSION
    task_name: str = ''
    success: bool = True
    message: str = ''

    def get_event_name(self) -> str:
        return WorkerEventNames.WORKER_COMPLETED


@dataclass
class WorkerAckEvent(Event):
    """Host 侧回执（背压用），v1.0 预留"""
    schema_version: str = SCHEMA_VERSION
    ack_count: int = 0

    def get_event_name(self) -> str:
        return WorkerEventNames.WORKER_ACK


@dataclass
class WorkerCrashReportEvent(Event):
    schema_version: str = SCHEMA_VERSION
    last_completed_task_id: str = ''
    in_progress_task_id: str = ''
    crash_reason: str = ''

    def get_event_name(self) -> str:
        return WorkerEventNames.WORKER_CRASH_REPORT


@dataclass
class WorkerBackpressureEvent(Event):
    schema_version: str = SCHEMA_VERSION
    queue_size: int = 0
    dropped: bool = False

    def get_event_name(self) -> str:
        return WorkerEventNames.WORKER_BACKPRESSURE