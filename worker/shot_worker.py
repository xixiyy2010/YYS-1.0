# worker/shot_worker.py
"""
截图子进程入口。

由 capability/screenshot.py 通过 subprocess.Popen 调用：
    python worker/shot_worker.py <hwnd> <shm_name>

位置：worker/ 目录。
BASE_DIR 指向项目根（worker 的上级），这样 logs/ 还是落在项目根下。

职责：
    - 循环截屏 → 写入共享内存
    - 父进程退出就自杀

改动要点（v4）：
    移除 templates_dir 参数。截图子进程与模板无关。
"""
import ctypes
import ctypes.wintypes
import numpy as np
import cv2
import time
import struct
import sys
import os
import threading
import logging
from multiprocessing import shared_memory

ctypes.windll.user32.SetProcessDPIAware()

# 项目根 = worker 的上级目录
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_DIR = os.path.join(BASE_DIR, 'logs')
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, 'shot_worker.log')
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    encoding='utf-8'
)


def _watch_parent():
    """后台线程：父进程退出就自杀"""
    parent_pid = os.getppid()
    kernel32 = ctypes.windll.kernel32
    while True:
        try:
            h = kernel32.OpenProcess(0x1000, False, parent_pid)
            if not h:
                os._exit(0)
            code = ctypes.c_ulong()
            ok = kernel32.GetExitCodeProcess(h, ctypes.byref(code))
            kernel32.CloseHandle(h)
            if not ok or code.value != 259:
                os._exit(0)
        except Exception:
            pass
        time.sleep(1.0)


def capture_window_printwindow(hwnd):
    """使用 PrintWindow 截取窗口客户区图像，返回 BGR 图像（numpy 数组）或 None"""
    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32

    rect = ctypes.wintypes.RECT()
    user32.GetClientRect(hwnd, ctypes.byref(rect))
    width = rect.right - rect.left
    height = rect.bottom - rect.top
    if width <= 0 or height <= 0:
        return None

    hdc = user32.GetWindowDC(hwnd)
    if not hdc:
        return None

    memdc = gdi32.CreateCompatibleDC(hdc)
    bitmap = gdi32.CreateCompatibleBitmap(hdc, width, height)
    if not memdc or not bitmap:
        user32.ReleaseDC(hwnd, hdc)
        return None

    old_bmp = gdi32.SelectObject(memdc, bitmap)

    result = user32.PrintWindow(hwnd, memdc, 2)
    if result == 0:
        gdi32.SelectObject(memdc, old_bmp)
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(memdc)
        user32.ReleaseDC(hwnd, hdc)
        return None

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [
            ("biSize", ctypes.wintypes.DWORD),
            ("biWidth", ctypes.wintypes.LONG),
            ("biHeight", ctypes.wintypes.LONG),
            ("biPlanes", ctypes.wintypes.WORD),
            ("biBitCount", ctypes.wintypes.WORD),
            ("biCompression", ctypes.wintypes.DWORD),
            ("biSizeImage", ctypes.wintypes.DWORD),
            ("biXPelsPerMeter", ctypes.wintypes.LONG),
            ("biYPelsPerMeter", ctypes.wintypes.LONG),
            ("biClrUsed", ctypes.wintypes.DWORD),
            ("biClrImportant", ctypes.wintypes.DWORD),
        ]
    bmi = BITMAPINFOHEADER()
    bmi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bmi.biWidth = width
    bmi.biHeight = -height
    bmi.biPlanes = 1
    bmi.biBitCount = 32
    bmi.biCompression = 0
    bmi.biSizeImage = width * height * 4

    buf = (ctypes.c_ubyte * (width * height * 4))()
    got_bits = gdi32.GetDIBits(memdc, bitmap, 0, height, buf, ctypes.byref(bmi), 0)

    gdi32.SelectObject(memdc, old_bmp)
    gdi32.DeleteObject(bitmap)
    gdi32.DeleteDC(memdc)
    user32.ReleaseDC(hwnd, hdc)

    if not got_bits:
        return None

    img = np.frombuffer(buf, dtype=np.uint8).reshape((height, width, 4))
    img = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
    return img


def main():
    threading.Thread(target=_watch_parent, daemon=True).start()

    if len(sys.argv) < 3:
        logging.error("参数不足，需要 hwnd 和 shm_name")
        return

    hwnd = int(sys.argv[1])
    shm_name = sys.argv[2]

    shm = shared_memory.SharedMemory(name=shm_name)
    logging.info(f"截图子进程启动，hwnd={hwnd}, shm={shm_name}")

    MAX_W = 3840
    MAX_H = 2160
    MAX_FRAME = MAX_W * MAX_H * 4
    DEFAULT_MODE_MS = 100   # mode 字段异常时的兜底周期

    while True:
        try:
            img_bgr = capture_window_printwindow(hwnd)
            if img_bgr is None:
                time.sleep(0.03)
                continue

            h, w = img_bgr.shape[:2]
            if w > MAX_W or h > MAX_H:
                logging.error(f"图像超出共享内存上限: {w}x{h} > {MAX_W}x{MAX_H}")
                time.sleep(0.1)
                continue

            img_bgra = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2BGRA)
            data_bytes = img_bgra.tobytes()
            if len(data_bytes) > MAX_FRAME:
                logging.error(f"数据超出上限: {len(data_bytes)} > {MAX_FRAME}")
                continue

            # 写宽度 / 高度 / 图像数据（从 12 偏移开始）
            shm.buf[0:4] = struct.pack('i', w)
            shm.buf[4:8] = struct.pack('i', h)
            shm.buf[12:12 + len(data_bytes)] = data_bytes

            # 读模式（毫秒），决定下一轮睡多久
            try:
                mode_ms = struct.unpack('i', shm.buf[8:12])[0]
            except Exception:
                mode_ms = DEFAULT_MODE_MS
            if mode_ms <= 0 or mode_ms > 1000:
                mode_ms = DEFAULT_MODE_MS
            time.sleep(mode_ms / 1000.0)

        except KeyboardInterrupt:
            logging.info("收到中断，退出")
            break
        except Exception as e:
            logging.error(f"Worker exception: {e}", exc_info=True)
            time.sleep(0.1)

    logging.info("截图子进程退出")
    shm.close()


if __name__ == "__main__":
    main()