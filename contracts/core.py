# contracts/core.py
"""
Contracts.Core

定义跨层的基础设施契约：

    - IEventBus          进程内发布/订阅接口
    - Event              所有事件的基类（带 Sender）
    - CoreEventNames     Core 层通用事件名
    - IPluginTask        插件任务接口（含 IsIdempotent）
    - DTO                TaskInfo / WorkerInfo
    - 通用事件           StateEnteredEvent / StateExitedEvent
    - 版本常量           CONTRACT_VERSION / SCHEMA_VERSION

StateEnteredEvent / StateExitedEvent 放在这里，因为它被 Worker 和 Host
两侧共同使用。Worker 编译期不允许引用 Contracts.Host。

依赖：无
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, Optional


# ==================================================================
#  版本常量
# ==================================================================

class ContractVersion:
    """
    契约版本。

    握手规则：
        MAJOR 不同 → 拒绝连接
        MINOR 不同 → 允许连接，启用兼容降级
        PATCH 不同 → 允许连接，无需处理
    """
    MAJOR = 1
    MINOR = 0
    PATCH = 0

    @classmethod
    def as_tuple(cls) -> tuple:
        return (cls.MAJOR, cls.MINOR, cls.PATCH)

    @classmethod
    def as_str(cls) -> str:
        return f'{cls.MAJOR}.{cls.MINOR}.{cls.PATCH}'


# 每个 DTO 的 SchemaVersion 字段用这个常量
SCHEMA_VERSION = '1.0.0'


# ==================================================================
#  事件基类
# ==================================================================

@dataclass
class Event:
    """
    所有事件的基类。

    字段：
        sender  发送者标识（通常用 device / worker 标识）
                订阅者应忽略自己发出的事件
    """
    sender: str = ''

    def get_event_name(self) -> str:
        """子类应覆盖，返回事件名字符串"""
        return self.__class__.__name__


# ==================================================================
#  IEventBus
# ==================================================================

class IEventBus(abc.ABC):
    """
    进程内事件总线接口。

    - 发布：publish(event)
    - 订阅：subscribe(event_type, handler)，返回 unsubscribe 句柄
    - 线程安全：实现方必须保证
    """

    @abc.abstractmethod
    def publish(self, event: Event) -> None:
        ...

    @abc.abstractmethod
    def subscribe(self, event_type: type, handler: Callable[[Event], None]
                  ) -> Callable[[], None]:
        """
        订阅某类事件。

        返回一个无参的取消订阅函数。
        """
        ...


# ==================================================================
#  IPluginTask
# ==================================================================

class IPluginTask(abc.ABC):
    """
    插件任务接口。

    Worker 崩溃后由 AutoScheduler 决定：
        IsIdempotent = True  → 重新下发
        IsIdempotent = False → 跳过到下一个
    """

    @property
    @abc.abstractmethod
    def name(self) -> str:
        """任务名（如 'collaboration' / 'yuling'）"""
        ...

    @property
    def is_idempotent(self) -> bool:
        """默认视为幂等；业务插件可覆盖"""
        return True

    @abc.abstractmethod
    def run(self) -> None:
        ...

    @abc.abstractmethod
    def stop(self) -> None:
        ...


# ==================================================================
#  通用枚举
# ==================================================================

class SharedMemoryState(str, Enum):
    ACTIVE = 'active'
    ORPHANED = 'orphaned'
    EXPIRED = 'expired'


class TaskState(str, Enum):
    STARTING = 'starting'
    RUNNING = 'running'
    PAUSED = 'paused'
    STOPPING = 'stopping'
    STOPPED = 'stopped'
    CRASHED = 'crashed'


# ==================================================================
#  DTO
# ==================================================================

@dataclass
class TaskInfo:
    schema_version: str = SCHEMA_VERSION
    task_name: str = ''
    device: str = ''
    is_idempotent: bool = True
    state: TaskState = TaskState.STOPPED


@dataclass
class WorkerInfo:
    schema_version: str = SCHEMA_VERSION
    worker_id: str = ''
    pid: int = 0
    contract_version: str = ContractVersion.as_str()
    device: str = ''


# ==================================================================
#  Core 层通用事件名
# ==================================================================

class CoreEventNames:
    # 截图就绪（ImageCenter 用）
    SCREENSHOT_READY = 'screenshot_ready'

    # 导航完成（NavigationEngine 用）
    NAVIGATION_COMPLETED = 'navigation_completed'

    # 任务进度
    TASK_PROGRESS = 'task_progress'

    # 日志（跨层统一日志通道）
    LOG_MESSAGE = 'log_message'

    # 状态进出（业务层通过它通知 AutoScheduler，通用）
    STATE_ENTERED = 'state_entered'
    STATE_EXITED = 'state_exited'


# ==================================================================
#  通用事件：状态进出
# ==================================================================

@dataclass
class StateEnteredEvent(Event):
    """
    业务层上报：某设备进入某状态。

    sender      = 设备标识
    state_name  = 业务层定义的字符串（比如 'tupo_8_2'）

    本事件被 Worker / Host / 主进程内部任何一侧使用，
    因此定义在 Contracts.Core。
    """
    state_name: str = ''

    def get_event_name(self) -> str:
        return CoreEventNames.STATE_ENTERED


@dataclass
class StateExitedEvent(Event):
    """业务层上报：某设备退出某状态。"""
    state_name: str = ''

    def get_event_name(self) -> str:
        return CoreEventNames.STATE_EXITED