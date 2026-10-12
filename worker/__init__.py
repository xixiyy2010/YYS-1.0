# worker/__init__.py
"""
Worker Layer：执行子进程侧组件

依赖：contracts.core / contracts.worker / infra.ipc_bridge
"""
from .shared_memory_accessor import SharedMemoryAccessor
from .worker_host import WorkerHost

__all__ = ['SharedMemoryAccessor', 'WorkerHost']