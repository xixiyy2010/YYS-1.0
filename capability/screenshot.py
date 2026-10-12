# capability/screenshot.py
"""
screenshot —— 截图子进程生命周期管理（机床能力）

改动要点（v4）：
    不再向 shot_worker 传递 templates_dir。
    截图子进程只负责截图 + 写共享内存，与模板无关。

子进程参数（不变）：
    python worker/shot_worker.py <hwnd> <shm_name>

依赖：无（只依赖标准库）
"""
from __future__ import annotations

import logging
import os
import struct
import subprocess
import sys
from multiprocessing import shared_memory
from typing import Optional, Tuple


logger = logging.getLogger(__name__)


class ScreenshotWorker:
    """
    截图子进程管理器。

    用法：
        worker = ScreenshotWorker(shot_hwnd, base_dir)
        if worker.start():
            shm_name = worker.shm_name
            proc = worker.proc
        ...
        worker.stop()
    """

    def __init__(self, shot_hwnd: int, base_dir: str,
                 shm_size: Tuple[int, int] = (3840, 2160),
                 script_name: str = 'worker/shot_worker.py'):
        self.shot_hwnd = shot_hwnd
        self.base_dir = base_dir
        self.shm_size = shm_size
        self.script_name = script_name

        self._shm: Optional[shared_memory.SharedMemory] = None
        self._proc: Optional[subprocess.Popen] = None

    # ==================================================================
    #  启停
    # ==================================================================

    def start(self) -> bool:
        """启动截图子进程。已启动返回 True；失败返回 False。"""
        if self.is_running():
            return True

        # 1. 创建共享内存
        try:
            w, h = self.shm_size
            size = 12 + w * h * 4          # 8 → 12（头部多了 4 字节的 mode 字段）
            self._shm = shared_memory.SharedMemory(create=True, size=size)
            self._shm.buf[0:4] = struct.pack('i', w)
            self._shm.buf[4:8] = struct.pack('i', h)
            self._shm.buf[8:12] = struct.pack('i', 100)   # 初始：空闲模式 100ms
        except Exception:
            logger.exception('ScreenshotWorker: 共享内存创建失败')
            self._shm = None
            return False

        # 2. 检查脚本
        script = os.path.join(self.base_dir, self.script_name)
        if not os.path.exists(script):
            logger.error('ScreenshotWorker: %s 不存在', script)
            self._release_shm()
            return False

        # 3. 起子进程
        try:
            self._proc = subprocess.Popen(
                [sys.executable, script,
                 str(self.shot_hwnd),
                 self._shm.name],
                cwd=self.base_dir,
            )
        except Exception:
            logger.exception('ScreenshotWorker: 子进程启动失败')
            self._release_shm()
            self._proc = None
            return False

        logger.info('ScreenshotWorker: 已启动 pid=%s shm=%s',
                    self._proc.pid, self._shm.name)
        return True

    def stop(self) -> None:
        """停止子进程 + 释放共享内存"""
        if self._proc is not None and self._proc.poll() is None:
            try:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
                    self._proc.wait(timeout=1.0)
            except Exception:
                logger.exception('ScreenshotWorker: 终止异常')
        self._proc = None

        self._release_shm()

    def _release_shm(self) -> None:
        if self._shm is not None:
            try:
                self._shm.close()
                self._shm.unlink()
            except Exception:
                logger.exception('ScreenshotWorker: 释放共享内存异常')
            self._shm = None

    # ==================================================================
    #  属性
    # ==================================================================

    @property
    def shm_name(self) -> Optional[str]:
        return self._shm.name if self._shm is not None else None

    @property
    def proc(self) -> Optional[subprocess.Popen]:
        return self._proc

    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None