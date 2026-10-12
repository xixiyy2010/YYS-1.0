# worker/click_worker.py
"""
点击子进程入口。

由 capability/device_utils.py 通过 subprocess.Popen 调用：
    python worker/click_worker.py click <hwnd> <x> <y>
    python worker/click_worker.py swipe <hwnd> <x1> <y1> <x2> <y2> <duration>

位置：worker/ 目录。
- BASE_DIR  = 项目根（worker 的上级）→ 用于定位 logs/
- DLL 同 worker/common/ClickDll.dll（跟着自己走）

职责：
    - 加载 ClickDll.dll
    - 转发一次点击 / 滑动
    - 用完即退
"""
import sys
import ctypes
import ctypes.wintypes
import os
import logging

ctypes.windll.user32.SetProcessDPIAware()

# 项目根 = worker 的上级目录（用于 logs/）
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 本文件所在目录 = worker/（用于定位同目录的 common/）
_HERE = os.path.dirname(os.path.abspath(__file__))

LOG_DIR = os.path.join(BASE_DIR, 'logs')
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, 'click_worker.log')
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    encoding='utf-8'
)


def main():
    if len(sys.argv) < 4:
        logging.error("参数不足")
        return

    action = sys.argv[1]          # "click" 或 "swipe"
    try:
        hwnd = int(sys.argv[2])
    except ValueError:
        logging.error(f"无效的窗口句柄: {sys.argv[2]}")
        return

    # DLL 跟着自己走：worker/common/ClickDll.dll
    dll_path = os.path.join(_HERE, "common", "ClickDll.dll")
    if not os.path.exists(dll_path):
        logging.error(f"未找到 ClickDll.dll: {dll_path}")
        return

    try:
        dll = ctypes.CDLL(dll_path)
    except Exception as e:
        logging.error(f"加载 ClickDll.dll 失败: {e}")
        return

    dll.Click.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
    dll.Click.restype = None
    dll.Swipe.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                          ctypes.c_int, ctypes.c_int, ctypes.c_int]
    dll.Swipe.restype = None

    try:
        if action == "click":
            if len(sys.argv) < 5:
                logging.error("click 缺少坐标参数")
                return
            x = int(sys.argv[3])
            y = int(sys.argv[4])
            logging.info(f"点击 hwnd={hwnd}, x={x}, y={y}")
            dll.Click(ctypes.c_void_p(hwnd), x, y)
        elif action == "swipe":
            if len(sys.argv) < 8:
                logging.error("swipe 缺少坐标参数")
                return
            x1 = int(sys.argv[3])
            y1 = int(sys.argv[4])
            x2 = int(sys.argv[5])
            y2 = int(sys.argv[6])
            duration = int(sys.argv[7])
            logging.info(f"滑动 hwnd={hwnd}, ({x1},{y1})->({x2},{y2}), duration={duration}")
            dll.Swipe(ctypes.c_void_p(hwnd), x1, y1, x2, y2, duration)
        else:
            logging.error(f"未知动作: {action}")
    except Exception as e:
        logging.error(f"调用 DLL 出错: {e}", exc_info=True)


if __name__ == "__main__":
    main()