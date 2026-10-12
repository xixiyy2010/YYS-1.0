# contracts/host.py
"""
Contracts.Host

Host 侧事件名与专属 DTO。

职责边界：
    - UI 意图事件（UI → Host）
    - 调度事件（AutoScheduler → 全局广播）
    - 配置热重载
    - Worker 生命周期
    - 共享内存孤儿事件
    - 设备生命周期（DeviceManager → UI）
    - 老板状态 / 座位信号（Boss → UI）

不包含：
    StateEnteredEvent / StateExitedEvent —— 在 contracts.core，
    因为 Worker 侧也上报这两个事件。

中立原则：
    座位信号走 SlotChangedEvent（通用）。
    slot_name 是座位名（"alert" / "switch_1" / ...），引擎原样搬运。

依赖：contracts.core
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .core import Event


# ==================================================================
#  Host 事件名
# ==================================================================

class HostEventNames:
    # ---- UI 意图（UI → Host） ----
    UI_START_TASK = 'ui_start_task'
    UI_STOP_TASK = 'ui_stop_task'
    UI_CONNECT_DEVICE = 'ui_connect_device'
    UI_DISCONNECT_DEVICE = 'ui_disconnect_device'

    # ---- 调度事件（AutoScheduler → 全局广播） ----
    PAUSE_RESOURCE = 'pause_resource'
    RESUME_RESOURCE = 'resume_resource'

    # ---- 配置热重载 ----
    CONFIG_RELOAD_REQUEST = 'config_reload_request'
    CONFIG_RELOAD_EXECUTE = 'config_reload_execute'
    CONFIG_RELOADED = 'config_reloaded'

    # ---- Worker 生命周期 ----
    WORKER_RESTART_SCHEDULED = 'worker_restart_scheduled'
    WORKER_CONNECTION_REJECTED = 'worker_connection_rejected'
    WORKER_PERMANENTLY_FAILED = 'worker_permanently_failed'

    # ---- 共享内存孤儿 ----
    SHARED_MEMORY_ORPHANED = 'shared_memory_orphaned'

    # ---- 设备生命周期（DeviceManager → UI） ----
    DEVICE_CONNECTED = 'device_connected'
    DEVICE_DISCONNECTED = 'device_disconnected'

    # ---- 老板状态（Boss → UI） ----
    BOSS_STATE_CHANGED = 'boss_state_changed'

    # ---- 座位信号（Boss → UI，通用） ----
    SLOT_CHANGED = 'slot_changed'


# ==================================================================
#  调度事件
# ==================================================================

@dataclass
class PauseResourceEvent(Event):
    """
    AutoScheduler 发布：请求暂停某资源。

    device         设备标识
    resource_name  资源名（业务层定义的字符串）

    订阅者响应条件：
        evt.device == self.device 且 evt.resource_name == 自己关心的资源
    """
    device: str = ''
    resource_name: str = ''

    def get_event_name(self) -> str:
        return HostEventNames.PAUSE_RESOURCE


@dataclass
class ResumeResourceEvent(Event):
    """AutoScheduler 发布：请求恢复某资源。"""
    device: str = ''
    resource_name: str = ''

    def get_event_name(self) -> str:
        return HostEventNames.RESUME_RESOURCE


# ==================================================================
#  Worker 生命周期事件
# ==================================================================

@dataclass
class WorkerRestartScheduledEvent(Event):
    worker_id: str = ''
    delay_s: float = 0.0
    attempt: int = 0

    def get_event_name(self) -> str:
        return HostEventNames.WORKER_RESTART_SCHEDULED


@dataclass
class WorkerConnectionRejectedEvent(Event):
    worker_id: str = ''
    reason: str = ''

    def get_event_name(self) -> str:
        return HostEventNames.WORKER_CONNECTION_REJECTED


@dataclass
class WorkerPermanentlyFailedEvent(Event):
    worker_id: str = ''
    reason: str = ''

    def get_event_name(self) -> str:
        return HostEventNames.WORKER_PERMANENTLY_FAILED


@dataclass
class SharedMemoryOrphanedEvent(Event):
    """由 WorkerLauncher 发布（SharedMemoryManager 只回调不发布）。"""
    shm_name: str = ''

    def get_event_name(self) -> str:
        return HostEventNames.SHARED_MEMORY_ORPHANED


# ==================================================================
#  设备生命周期事件
# ==================================================================

@dataclass
class DeviceConnectedEvent(Event):
    """
    DeviceManager 发布：设备已绑定。
    UI 订阅此事件创建 / 更新 Tab。
    """
    device: str = ''
    window_title: str = ''      # 契约字段名不动；引擎侧叫 target_title
    emulator_type: str = ''     # 契约字段名不动；引擎侧叫 adapter_key
    click_hwnd: int = 0
    shot_hwnd: int = 0
    window_w: int = 0
    window_h: int = 0

    def get_event_name(self) -> str:
        return HostEventNames.DEVICE_CONNECTED


@dataclass
class DeviceDisconnectedEvent(Event):
    """DeviceManager 发布：设备已解绑。UI 订阅此事件更新 Tab。"""
    device: str = ''

    def get_event_name(self) -> str:
        return HostEventNames.DEVICE_DISCONNECTED


# ==================================================================
#  老板状态事件
# ==================================================================

@dataclass
class BossStateChangedEvent(Event):
    """
    老板状态变化。
    UI 订阅此事件，把对应 Tab 变色（运行→绿，出错→红）。
    """
    device: str = ''
    state: str = ''      # 'idle' / 'running' / 'hanging' / 'error' / 'stopping'
    detail: str = ''

    def get_event_name(self) -> str:
        return HostEventNames.BOSS_STATE_CHANGED


# ==================================================================
#  座位信号事件（通用）
# ==================================================================

@dataclass
class SlotChangedEvent(Event):
    """
    座位信号。老板上报 → 引擎转发给 UI。

    device     设备标识
    slot_name  座位名（"alert" / "switch_1" / "switch_2" / ...）
    value      值（提醒位："blink" / "off"；开关位：True / False）

    举例：
        老板让提醒位闪：    SlotChangedEvent(device=..., slot_name='alert',
                                            value='blink')
        老板报开关1打开：   SlotChangedEvent(device=..., slot_name='switch_1',
                                            value=True)

    引擎只搬运，不解析 slot_name 的含义。
    """
    device: str = ''
    slot_name: str = ''
    value: Any = None

    def get_event_name(self) -> str:
        return HostEventNames.SLOT_CHANGED