# capability/device_utils.py
"""
device_utils —— 设备 IO 能力（机床）

改动要点（v4）：
    - 基准分辨率从全局单值改为 per-device。
      之前 _active_base_res 是全局变量，多设备时后绑定的会覆盖先绑定的，
      导致先绑定设备的 tap 坐标缩放系数被错用。
      现在新增 _active_base_res_by_device，按 device 隔离；
      保留全局 _active_base_res 作为 fallback，兼容旧调用方。

其他职责（不变）：
    - 共享内存创建/读取（截图帧）
    - 点击 / 滑动（转发 click_worker.py 调 DLL）
    - 句柄 / 窗口尺寸注册
    - 基准分辨率同步

依赖：capability.resource_paths
"""
import os
import random
import time
import subprocess
import numpy as np
import cv2
import ctypes
import logging
import sys
import struct
from multiprocessing import shared_memory

from . import resource_paths

_shared_mem_by_device = {}
_window_size_map = {}
_render_hwnd_map = {}
_device_hwnd_map = {}

# 兼容旧调用方：不传 device 时用全局值
_active_base_res = None
# 按设备隔离的基准分辨率
_active_base_res_by_device = {}


def set_active_base_resolution(w, h, device=None):
    """
    设置基准分辨率。

    device 非空 → 存到 per-device 字典；
    device 为空 → 存到全局，兼容旧调用方。
    """
    global _active_base_res
    if not (w and h and w > 0 and h > 0):
        return
    res = (int(w), int(h))
    if device:
        _active_base_res_by_device[device] = res
    else:
        _active_base_res = res


def get_active_base_resolution(device=None):
    """
    读基准分辨率。

    device 非空 → 优先返回该设备的值，没有就回退全局；
    device 为空 → 返回全局。
    """
    if device:
        return _active_base_res_by_device.get(device) or _active_base_res
    return _active_base_res


def _scan_bases(templates_dir):
    import json as _json
    result = []
    if not templates_dir or not os.path.isdir(templates_dir):
        return result
    for f in os.listdir(templates_dir):
        if not (f.startswith('source_') and f.endswith('.json')):
            continue
        p = os.path.join(templates_dir, f)
        try:
            with open(p, 'r', encoding='utf-8') as fp:
                data = _json.load(fp)
            res = data.get('meta', {}).get('resolution')
            if res and len(res) == 2:
                result.append((int(res[1]), int(res[0]), p))
        except Exception as e:
            logging.warning(f'[device_utils] 读取基准失败 {p}: {e}')
    return result


def sync_base_resolution_from_index(device=None, target_h=None, templates_dir=None):
    """
    从 templates 目录里挑最接近当前窗口高度的基准，设为当前基准。
    templates_dir 未传时，从 resource_paths 读。
    """
    try:
        if templates_dir is None:
            templates_dir = resource_paths.get_templates_dir()

        if not templates_dir:
            logging.warning(
                '[device_utils] 未设置 templates 目录，'
                '请先调用 resource_paths.set_templates_dir()')
            return False

        if target_h is None and device:
            size = _window_size_map.get(device)
            if size:
                target_h = size[1]

        candidates = _scan_bases(templates_dir)
        if not candidates:
            logging.warning(f'[device_utils] 未找到任何基准: {templates_dir}')
            return False

        if target_h is None:
            chosen = max(candidates, key=lambda x: x[0])
        else:
            chosen = min(candidates, key=lambda x: abs(x[0] - target_h))

        base_h, base_w, path = chosen
        set_active_base_resolution(base_w, base_h, device=device)
        logging.info(
            f'[device_utils] 基准选择: {base_w}x{base_h} '
            f'({os.path.basename(path)}, 目标高={target_h}, device={device})')
        return True
    except Exception as e:
        logging.warning(f'[device_utils] 选基准失败: {e}')
    return False


def set_window_size(device, w, h):
    if device and w > 0 and h > 0:
        _window_size_map[device] = (w, h)


def get_window_size(device):
    return _window_size_map.get(device, None)


def set_device_render_hwnd(device, hwnd):
    if hwnd:
        _render_hwnd_map[device] = hwnd
    else:
        _render_hwnd_map.pop(device, None)


def get_render_hwnd(device):
    return _render_hwnd_map.get(device)


def set_device_hwnd(serial, hwnd):
    if hwnd:
        _device_hwnd_map[serial] = hwnd
    else:
        _device_hwnd_map.pop(serial, None)


def register_shared_memory(device, shm_name):
    if device:
        _shared_mem_by_device[device] = shared_memory.SharedMemory(name=shm_name)


def unregister_shared_memory(device):
    if device in _shared_mem_by_device:
        try:
            _shared_mem_by_device[device].close()
        except Exception:
            pass
        del _shared_mem_by_device[device]


_shared_mem = None


def init_shared_memory(shm_name):
    global _shared_mem
    _shared_mem = shared_memory.SharedMemory(name=shm_name)


def read_shared_frame(device=None):
    global _shared_mem
    if device:
        shm = _shared_mem_by_device.get(device)
    else:
        shm = _shared_mem
    if shm is None:
        return None
    try:
        width = struct.unpack('i', shm.buf[0:4])[0]
        height = struct.unpack('i', shm.buf[4:8])[0]
        data = bytes(shm.buf[12:12 + width * height * 4])   # ← 8 改 12
        img = np.frombuffer(data, dtype=np.uint8,
                            count=width * height * 4).reshape((height, width, 4))
        return cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
    except Exception as e:
        logging.warning(f"读取共享内存失败: {e}")
        return None

def set_shot_mode(device, mode_ms):
    """
    设置本设备的截图刷新周期（毫秒）。

    - 30  = 快模式（任务运行中）
    - 100 = 空闲模式（无任务）

    shot_worker 子进程每轮循环读这个值决定 sleep 长度。
    返回 True 成功，False 设备未注册 / 写失败。
    """
    shm = _shared_mem_by_device.get(device)
    if shm is None:
        return False
    try:
        shm.buf[8:12] = struct.pack('i', int(mode_ms))
        return True
    except Exception as e:
        logging.warning(f"设置截图模式失败 device={device}: {e}")
        return False

# ---------- 点击 DLL ----------

def _click_worker_path():
    """返回 worker/click_worker.py 的绝对路径"""
    return os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "worker", "click_worker.py")


def click_cpp(hwnd, x, y):
    try:
        script = _click_worker_path()
        result = subprocess.run(
            [sys.executable, script, "click", str(hwnd), str(x), str(y)],
            timeout=5,
            capture_output=True,
            text=True
        )
        if result.returncode != 0:
            logging.warning(f"click_cpp失败: {result.stderr}")
        return result.returncode == 0
    except Exception as e:
        logging.error(f"click_cpp异常: {e}")
        return False


def swipe_cpp(hwnd, x1, y1, x2, y2, duration_ms):
    try:
        script = _click_worker_path()
        result = subprocess.run(
            [sys.executable, script, "swipe", str(hwnd),
             str(x1), str(y1), str(x2), str(y2), str(duration_ms)],
            timeout=5,
            capture_output=True,
            text=True
        )
        if result.returncode != 0:
            logging.warning(f"swipe_cpp失败: {result.stderr}")
        return result.returncode == 0
    except Exception as e:
        logging.error(f"swipe_cpp异常: {e}")
        return False


def take_screenshot_np(device=None):
    return read_shared_frame(device)


def find_render_child(parent_hwnd):
    user32 = ctypes.windll.user32
    found = {'render': 0, 'nemu': 0}

    def enum_children(hwnd):
        child = user32.FindWindowExW(hwnd, None, None, None)
        while child:
            cls = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(child, cls, 256)
            title = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(child, title, 256)
            if cls.value == 'RenderWindow' and not found['render']:
                found['render'] = int(child)
                return
            if title.value == 'nemudisplay' and not found['nemu']:
                found['nemu'] = int(child)
            enum_children(child)
            if found['render']:
                return
            child = user32.FindWindowExW(hwnd, child, None, None)

    enum_children(parent_hwnd)
    return found['render'] or found['nemu'] or 0


def _get_click_hwnd(device):
    parent_hwnd = _device_hwnd_map.get(device)
    if not parent_hwnd:
        return None
    render_hwnd = get_render_hwnd(device)
    if not render_hwnd:
        render_hwnd = find_render_child(parent_hwnd)
        if render_hwnd:
            set_device_render_hwnd(device, render_hwnd)
    return render_hwnd or parent_hwnd


def tap(device, x, y):
    click_hwnd = _get_click_hwnd(device)
    if not click_hwnd:
        logging.warning(f"未找到点击窗口 device={device}")
        return False

    actual_size = _window_size_map.get(device)
    base_res = get_active_base_resolution(device)      # ← 改：per-device
    if base_res and actual_size:
        base_w, base_h = base_res
        actual_w, actual_h = actual_size
        actual_x = int(x * actual_w / base_w)
        actual_y = int(y * actual_h / base_h)
    else:
        actual_x, actual_y = x, y

    ok = click_cpp(click_hwnd, actual_x, actual_y)
    if not ok:
        logging.warning(f"点击失败 hwnd={click_hwnd} ({actual_x},{actual_y})")
    return ok


def swipe(device, x1, y1, x2, y2, duration):
    click_hwnd = _get_click_hwnd(device)
    if not click_hwnd:
        logging.warning(f"设备 {device} 未找到有效窗口句柄，无法滑动")
        return False

    actual_size = _window_size_map.get(device)
    base_res = get_active_base_resolution(device)      # ← 改：per-device
    if base_res and actual_size:
        base_w, base_h = base_res
        actual_w, actual_h = actual_size
        ax1 = int(x1 * actual_w / base_w)
        ay1 = int(y1 * actual_h / base_h)
        ax2 = int(x2 * actual_w / base_w)
        ay2 = int(y2 * actual_h / base_h)
    else:
        ax1, ay1, ax2, ay2 = x1, y1, x2, y2

    ok = swipe_cpp(click_hwnd, ax1, ay1, ax2, ay2, duration)
    if not ok:
        logging.warning(f"滑动失败: device={device}, hwnd={click_hwnd}")
    return ok


def normal_click_in_region(region):
    x1, y1, x2, y2 = region
    w = x2 - x1
    h = y2 - y1

    if w <= 2 or h <= 2:
        return int((x1 + x2) / 2), int((y1 + y2) / 2)

    center_x = (x1 + x2) / 2
    center_y = (y1 + y2) / 2
    sigma_x = max(1, w / 6)
    sigma_y = max(1, h / 6)

    base_x, base_y = center_x, center_y
    for _ in range(30):
        bx = random.gauss(center_x, sigma_x)
        by = random.gauss(center_y, sigma_y)
        if x1 + 1 <= bx <= x2 - 1 and y1 + 1 <= by <= y2 - 1:
            base_x, base_y = bx, by
            break

    offset_x = max(-3, min(3, random.gauss(0, 1.5)))
    offset_y = max(-3, min(3, random.gauss(0, 1.5)))

    final_x = int(base_x + offset_x)
    final_y = int(base_y + offset_y)
    final_x = max(x1, min(x2 - 1, final_x))
    final_y = max(y1, min(y2 - 1, final_y))

    return final_x, final_y