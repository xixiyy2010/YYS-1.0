# capability/__init__.py
"""
capability —— 机床能力层

放"业务无关的通用能力"。

当前包含：
    image_center   多基准模板管理器
    hwnd           Win32 窗口操作封装
    screenshot     截图子进程生命周期管理

依赖：无
被依赖：plugins.yys._shared / worker / host / ui
"""
from .image_center import (
    TemplateManager,
    expand_grid,
    grid_sample_point,
    compute_swipe_paths,
)
from .screenshot import ScreenshotWorker
from . import hwnd

__all__ = [
    'TemplateManager',
    'expand_grid',
    'grid_sample_point',
    'compute_swipe_paths',
    'ScreenshotWorker',
    'hwnd',
]