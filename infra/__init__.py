# infra/__init__.py
"""
基础设施层：EventBus、IPC Bridge、日志等跨层通用组件。

依赖：contracts.*
被依赖：worker.* / host.* / 业务层

变更（v2）：
    移除 SharedMemoryManager 导出。该类未被任何模块使用，
    共享内存的实际生命周期由 capability.screenshot.ScreenshotWorker
    和 worker.shared_memory_accessor.SharedMemoryAccessor 管理。
"""
from .event_bus import EventBus, get_event_bus, set_event_bus
from .ipc_bridge import (
    IPCServer, IPCClient,
    serialize_event, deserialize_event,
)
from .log_manager import LogManager

__all__ = [
    'EventBus', 'get_event_bus', 'set_event_bus',
    'IPCServer', 'IPCClient',
    'serialize_event', 'deserialize_event',
    'LogManager',
]