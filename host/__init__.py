# host/__init__.py
"""
Host 层：组合根

职责：
    - 创建 EventBus / IPC Server / Adapters
    - 扫描插件
    - 拉起 Worker
    - 启动 AutoScheduler

依赖：全部（组合根特权）
"""
from .plugin_loader import PluginLoader, PluginInfo
from .worker_launcher import WorkerLauncher

__all__ = ['PluginLoader', 'PluginInfo', 'WorkerLauncher']