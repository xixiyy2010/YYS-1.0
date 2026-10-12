# capability/resource_paths.py
"""
ResourcePaths —— 机床能力的资源路径注册表

机床不认识业务，也不知道模板放在哪里。
业务层在启动时（或子进程入口处）通过 set_templates_dir() 告诉机床：
"模板在这个绝对路径下"。
之后机床的组件（image_center / device_utils 等）从这里读。

设计：
    一个进程一个实例。主进程、截图子进程、worker 子进程各自独立。
    未设置时 get_templates_dir() 返回 None，调用方自己决定兜底策略。

依赖：无（只用标准库）
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Optional


logger = logging.getLogger(__name__)

_lock = threading.RLock()
_templates_dir: Optional[str] = None


def set_templates_dir(path: str) -> bool:
    """
    设置模板目录（绝对路径）。
    返回 True 表示设置成功（路径存在），False 表示路径不存在或参数无效。
    """
    global _templates_dir
    if not path:
        return False
    try:
        abs_path = os.path.abspath(path)
    except Exception:
        return False
    if not os.path.isdir(abs_path):
        logger.warning('resource_paths: templates 目录不存在: %s', abs_path)
        return False
    with _lock:
        _templates_dir = abs_path
    logger.info('resource_paths: templates 目录 = %s', abs_path)
    return True


def get_templates_dir() -> Optional[str]:
    """读当前 templates 目录。未设置返回 None。"""
    with _lock:
        return _templates_dir


def clear() -> None:
    """清空（测试 / 关闭时用）"""
    global _templates_dir
    with _lock:
        _templates_dir = None