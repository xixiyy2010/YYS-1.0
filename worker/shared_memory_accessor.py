# worker/shared_memory_accessor.py
"""
SharedMemoryAccessor —— Worker 侧的共享内存访问器

职责（v5.0）：
    - 每次启动从 IPC 拿到"新"共享内存名称
    - map 到本地
    - 只借用，不拥有生命周期
    - 只读（写入由 shot_worker 负责）
    - 每次读到新帧后更新 LastWriteTime —— v1.0 暂不做（等 SharedMemoryManager 落地）

依赖：contracts.core
"""
from __future__ import annotations

import logging
import struct
import threading
from multiprocessing import shared_memory
from typing import Optional

import cv2
import numpy as np


logger = logging.getLogger(__name__)


class SharedMemoryAccessor:
    """共享内存读取器（Worker 侧）"""

    def __init__(self):
        self._shm: Optional[shared_memory.SharedMemory] = None
        self._name: Optional[str] = None
        self._lock = threading.Lock()

    # ---------- 生命周期 ----------

    def attach(self, name: str) -> bool:
        """绑定到共享内存。name 为空或绑定失败返回 False。"""
        if not name:
            return False
        with self._lock:
            self._detach_locked()
            try:
                self._shm = shared_memory.SharedMemory(name=name)
                self._name = name
                logger.info('SharedMemoryAccessor: 已绑定 %s', name)
                return True
            except Exception:
                logger.exception('SharedMemoryAccessor: 绑定失败 %s', name)
                self._shm = None
                self._name = None
                return False

    def detach(self) -> None:
        with self._lock:
            self._detach_locked()

    def _detach_locked(self) -> None:
        if self._shm is not None:
            try:
                self._shm.close()
            except Exception:
                pass
            self._shm = None
        self._name = None

    @property
    def attached(self) -> bool:
        return self._shm is not None

    @property
    def name(self) -> Optional[str]:
        return self._name

    # ---------- 读取 ----------

    def read_frame_bgr(self) -> Optional[np.ndarray]:
        """
        从共享内存读取一帧。

        头部 12 字节：[width:int32][height:int32][mode:int32]
        尾部 width*height*4 字节 BGRA

        返回 BGR numpy 数组，失败返回 None。
        """
        with self._lock:
            shm = self._shm
        if shm is None:
            return None
        try:
            width = struct.unpack('i', shm.buf[0:4])[0]
            height = struct.unpack('i', shm.buf[4:8])[0]
            if width <= 0 or height <= 0:
                return None
            need = width * height * 4
            data = bytes(shm.buf[12:12 + need])   # ← 8 改 12
            img = np.frombuffer(data, dtype=np.uint8).reshape(
                (height, width, 4))
            return cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
        except Exception:
            logger.exception('SharedMemoryAccessor: 读取失败')
            return None