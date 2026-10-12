# infra/log_manager.py
"""
LogManager —— 独立日志管理（机床能力）

职责：
    1. 接收业务层的日志调用（info / warning / error）
    2. 落盘（按设备分文件 + 全局文件）
    3. 通知所有已注册的观察者（UI 订阅日志显示）

不职责：
    - 不碰 EventBus（事件通道与日志通道解耦）
    - 不解析日志内容
    - 不判断级别是否要显示（观察者自己决定）

观察者回调签名：
    callback(device: str, level: str, message: str)

依赖：无（只依赖标准库）
"""
from __future__ import annotations

import os
import threading
import time
from typing import Callable, Dict, List, Optional


Listener = Callable[[str, str, str], None]


class LogManager:
    """独立日志管理器（单例由 Host 创建，全局共享）"""

    def __init__(self, log_dir: str):
        self._log_dir = log_dir
        os.makedirs(self._log_dir, exist_ok=True)

        self._lock = threading.RLock()
        self._listeners: List[Listener] = []

        # 每设备日志文件句柄：device → file object
        self._device_files: Dict[str, object] = {}

        # 全局日志文件
        self._global_path = os.path.join(self._log_dir, 'app.log')

    # ==================================================================
    #  观察者注册
    # ==================================================================

    def add_listener(self, cb: Listener) -> None:
        """注册日志观察者（UI 层用）。重复注册无效果。"""
        if cb is None:
            return
        with self._lock:
            if cb not in self._listeners:
                self._listeners.append(cb)

    def remove_listener(self, cb: Listener) -> None:
        with self._lock:
            try:
                self._listeners.remove(cb)
            except ValueError:
                pass

    def clear_listeners(self) -> None:
        with self._lock:
            self._listeners.clear()

    # ==================================================================
    #  业务接口（业务层调这三个就够）
    # ==================================================================

    def info(self, device: str, message: str,
             ui_message: str = None, quiet: bool = False) -> None:
        self._emit(device, 'info', message, ui_message, quiet)

    def warning(self, device: str, message: str,
                ui_message: str = None, quiet: bool = False) -> None:
        self._emit(device, 'warning', message, ui_message, quiet)

    def error(self, device: str, message: str,
              ui_message: str = None, quiet: bool = False) -> None:
        self._emit(device, 'error', message, ui_message, quiet)

    # ==================================================================
    #  内部
    # ==================================================================

    def _emit(self, device: str, level: str, message: str,
              ui_message: str = None, quiet: bool = False) -> None:
        device = device or ''
        message = message if message is not None else ''
        ui = ui_message if ui_message is not None else message

        # 1. 落盘（永远执行，quiet 也落盘）
        try:
            self._write_file(device, level, message)
        except Exception:
            pass

        # 2. quiet 时跳过 UI 通知
        if quiet:
            return

        # 3. 通知观察者（用 ui_message）
        with self._lock:
            listeners = list(self._listeners)
        for cb in listeners:
            try:
                cb(device, level, ui)
            except Exception:
                pass

    def _write_file(self, device: str, level: str, message: str) -> None:
        ts = time.strftime('%Y-%m-%d %H:%M:%S')
        line = f'{ts} [{level.upper()}] {message}\n'

        # 全局文件
        with open(self._global_path, 'a', encoding='utf-8') as f:
            f.write(line)

        # 设备文件
        if device:
            safe = ''.join(
                c if c.isalnum() or c in '-_' else '_' for c in device)
            path = os.path.join(self._log_dir, f'{safe}.log')
            with open(path, 'a', encoding='utf-8') as f:
                f.write(line)

    # ==================================================================
    #  关闭
    # ==================================================================

    def shutdown(self) -> None:
        with self._lock:
            self._listeners.clear()
            self._device_files.clear()