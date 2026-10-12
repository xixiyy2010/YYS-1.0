# infra/shared_memory_manager.py
"""
SharedMemoryManager —— 共享内存块分配器

v1.0：只有 alloc / release。
    - 不维护状态机（Active/Orphaned/Expired 留给 v5.0）
    - 不做巡检
    - 不做 mark_orphaned
    - 不做 on_orphaned 回调

依赖：contracts.core
"""
from __future__ import annotations

import logging
import struct
import threading
from multiprocessing import shared_memory
from typing import Dict, Optional


logger = logging.getLogger(__name__)


class SharedMemoryManager:
    """共享内存块分配器（v1.0 简化版）"""

    def __init__(self):
        self._lock = threading.RLock()
        self._blocks: Dict[str, shared_memory.SharedMemory] = {}

    def alloc(self, width: int, height: int) -> Optional[str]:
        """分配一块共享内存，返回名字"""
        try:
            size = 8 + width * height * 4
            shm = shared_memory.SharedMemory(create=True, size=size)
            shm.buf[0:4] = struct.pack('i', width)
            shm.buf[4:8] = struct.pack('i', height)
            with self._lock:
                self._blocks[shm.name] = shm
            logger.info('SharedMemoryManager: 分配 %s (%dx%d)',
                        shm.name, width, height)
            return shm.name
        except Exception:
            logger.exception('SharedMemoryManager: 分配失败')
            return None

    def get(self, name: str) -> Optional[shared_memory.SharedMemory]:
        with self._lock:
            return self._blocks.get(name)

    def release(self, name: str) -> None:
        with self._lock:
            shm = self._blocks.pop(name, None)
        if shm is None:
            return
        try:
            shm.close()
            shm.unlink()
            logger.info('SharedMemoryManager: 释放 %s', name)
        except Exception:
            logger.exception('SharedMemoryManager: 释放失败 %s', name)

    def release_all(self) -> None:
        with self._lock:
            names = list(self._blocks.keys())
        for n in names:
            self.release(n)