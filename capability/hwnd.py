# capability/hwnd.py
"""
hwnd —— 纯 Win32 窗口操作封装（机床能力）

不知道任何具体窗口标题 / 类名 / 模拟器类型。
只提供"怎么找"的通用能力，不关心"找哪个"。

对外能力：
    enum_top_windows()              枚举所有顶层窗口
    find_top_window_by_title()      按标题找顶层窗口
    find_child_by_title()           按标题找子窗口
    find_child_by_class()           按类名找子窗口
    find_descendant_by_class_or_title()  混合查找
    get_client_size()               取客户区尺寸
    is_window() / get_window_title() / activate_window()
    resolve_spec()                  把一条 spec 规则解释成句柄
    resolve_handles()               把整个适配器 spec 解释成句柄组

依赖：无
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes
import logging
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ==================================================================
#  顶层窗口枚举
# ==================================================================

def enum_top_windows() -> List[Tuple[int, str]]:
    """
    枚举所有顶层窗口。

    返回 [(hwnd, title), ...]，只返回有标题的窗口。
    """
    user32 = ctypes.windll.user32
    results: List[Tuple[int, str]] = []

    WNDENUMPROC = ctypes.WINFUNCTYPE(
        ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)

    def callback(hwnd, lParam):
        try:
            length = user32.GetWindowTextLengthW(hwnd)
            if length > 0:
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buf, length + 1)
                results.append((int(hwnd), buf.value))
        except Exception:
            pass
        return True

    user32.EnumWindows(WNDENUMPROC(callback), 0)
    return results


def find_top_window_by_title(title: str) -> int:
    """按完整标题查顶层窗口，找不到返回 0"""
    if not title:
        return 0
    try:
        return int(ctypes.windll.user32.FindWindowW(None, title))
    except Exception:
        return 0


# ==================================================================
#  子窗口查找
# ==================================================================

def find_child_by_title(parent_hwnd: int, title: str) -> int:
    """
    递归查找指定标题的子窗口，返回第一个匹配句柄（0 = 未找到）。
    """
    if not parent_hwnd:
        return 0
    user32 = ctypes.windll.user32
    found = []

    WNDENUMPROC = ctypes.WINFUNCTYPE(
        ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)

    def callback(hwnd, lParam):
        try:
            length = user32.GetWindowTextLengthW(hwnd)
            if length > 0:
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buf, length + 1)
                if buf.value == title:
                    found.append(int(hwnd))
                    return False
        except Exception:
            pass
        return True

    user32.EnumChildWindows(
        ctypes.wintypes.HWND(parent_hwnd), WNDENUMPROC(callback), 0)
    return found[0] if found else 0


def find_child_by_class(parent_hwnd: int, class_name: str) -> int:
    """
    递归查找指定类名的子窗口，返回第一个匹配句柄（0 = 未找到）。
    """
    if not parent_hwnd:
        return 0
    user32 = ctypes.windll.user32
    found = []

    WNDENUMPROC = ctypes.WINFUNCTYPE(
        ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)

    def callback(hwnd, lParam):
        try:
            buf = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, buf, 256)
            if buf.value == class_name:
                found.append(int(hwnd))
                return False
        except Exception:
            pass
        return True

    user32.EnumChildWindows(
        ctypes.wintypes.HWND(parent_hwnd), WNDENUMPROC(callback), 0)
    return found[0] if found else 0


def find_descendant_by_class_or_title(
    parent_hwnd: int,
    class_name: Optional[str] = None,
    title: Optional[str] = None,
) -> int:
    """
    深度优先查找：按类名匹配 or 按标题匹配，先命中先返回。
    两者都传时优先类名。
    """
    if not parent_hwnd:
        return 0
    user32 = ctypes.windll.user32
    found = []

    def enum_children(hwnd):
        child = user32.FindWindowExW(hwnd, None, None, None)
        while child:
            try:
                if class_name:
                    cls_buf = ctypes.create_unicode_buffer(256)
                    user32.GetClassNameW(child, cls_buf, 256)
                    if cls_buf.value == class_name:
                        found.append(int(child))
                        return True
                if title:
                    t_buf = ctypes.create_unicode_buffer(256)
                    user32.GetWindowTextW(child, t_buf, 256)
                    if t_buf.value == title:
                        found.append(int(child))
                        return True
                if enum_children(child):
                    return True
            except Exception:
                pass
            child = user32.FindWindowExW(hwnd, child, None, None)
        return False

    enum_children(parent_hwnd)
    return found[0] if found else 0


# ==================================================================
#  尺寸 / 有效性
# ==================================================================

def get_client_size(hwnd: int) -> Tuple[int, int]:
    """返回客户区尺寸 (w, h)；失败返回 (0, 0)"""
    if not hwnd:
        return 0, 0
    try:
        rect = ctypes.wintypes.RECT()
        ctypes.windll.user32.GetClientRect(
            ctypes.wintypes.HWND(hwnd), ctypes.byref(rect))
        return rect.right - rect.left, rect.bottom - rect.top
    except Exception:
        return 0, 0


def is_window(hwnd: int) -> bool:
    """窗口句柄是否有效"""
    if not hwnd:
        return False
    try:
        return bool(ctypes.windll.user32.IsWindow(
            ctypes.wintypes.HWND(hwnd)))
    except Exception:
        return False


def get_window_title(hwnd: int) -> str:
    """取窗口标题；失败返回空串"""
    if not hwnd:
        return ''
    try:
        user32 = ctypes.windll.user32
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return ''
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        return buf.value
    except Exception:
        return ''


def activate_window(hwnd: int) -> bool:
    """
    把窗口置前。

    用于 UI 双击 Tab 时激活设备窗口。

    Windows 限制：后台进程不能直接抢前台。
    绕过方法：AttachThreadInput 把当前线程附到前台线程的输入队列，
    借它的权限来设置前台窗口。
    """
    if not hwnd:
        return False
    try:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        # 如果窗口最小化，先恢复
        user32.ShowWindow(ctypes.wintypes.HWND(hwnd), 9)  # SW_RESTORE

        # 已在前台，直接返回
        fg_hwnd = user32.GetForegroundWindow()
        if fg_hwnd == hwnd:
            return True

        # 拿前台窗口所在线程 ID
        fg_thread = user32.GetWindowThreadProcessId(
            ctypes.wintypes.HWND(fg_hwnd), None)
        cur_thread = kernel32.GetCurrentThreadId()

        attached = False
        if fg_thread and fg_thread != cur_thread:
            attached = bool(user32.AttachThreadInput(
                cur_thread, fg_thread, True))

        try:
            user32.BringWindowToTop(ctypes.wintypes.HWND(hwnd))
            user32.SetForegroundWindow(ctypes.wintypes.HWND(hwnd))
        finally:
            if attached:
                user32.AttachThreadInput(cur_thread, fg_thread, False)

        return True
    except Exception:
        return False


# ==================================================================
#  spec 解释器（读业务规则 → 调本模块的查找函数）
# ==================================================================

def resolve_spec(parent_hwnd: int, spec: dict,
                 ctx: Optional[Dict[str, int]] = None,
                 _depth: int = 0) -> int:
    """
    按 spec 规则解析一个句柄。

    spec 格式：
        {"by": "title"|"class"|"parent"|"shot"|"click",
         "value": "...", "fallback": "..."}

    ctx 里存放已经解析出的句柄（供 by=shot/click 引用）：
        {"parent": hwnd, "shot": hwnd, "click": hwnd}

    返回句柄（int），找不到返回 0。

    _depth 是内部参数，防 fallback 链环递归。超过 3 层直接放弃。
    """
    if _depth > 3:
        logger.warning(
            'hwnd.resolve_spec: fallback 链超过 3 层，放弃。spec=%r', spec)
        return 0

    if not spec:
        return 0
    ctx = ctx or {}
    by = spec.get('by', '')
    value = spec.get('value', '')
    parent = ctx.get('parent', parent_hwnd)

    h = 0
    if by == 'parent':
        h = parent
    elif by == 'shot':
        h = ctx.get('shot', 0)
    elif by == 'click':
        h = ctx.get('click', 0)
    elif by == 'title':
        h = find_child_by_title(parent, value)
    elif by == 'class':
        h = find_child_by_class(parent, value)

    if h:
        return h

    fallback = spec.get('fallback')
    if not fallback:
        return 0
    # 递归时只保留 by，丢掉 value 和 fallback。
    # 效果是 fallback 链最长 1 层，天然防环。
    return resolve_spec(parent_hwnd, {'by': fallback}, ctx, _depth + 1)


def resolve_handles(parent_hwnd: int, adapter_spec: dict
                    ) -> Optional[Tuple[int, int, int, int]]:
    """
    解析整个适配器 spec，返回 (shot_hwnd, click_hwnd, width, height)。
    失败返回 None。

    adapter_spec 格式（来自 adapters.json 的一条）：
        {
          "window_keyword": "...",
          "shot":  {"by": ..., "value": ..., "fallback": ...},
          "click": {"by": ..., "value": ..., "fallback": ...}
        }
    """
    if not parent_hwnd or not adapter_spec:
        return None

    ctx = {'parent': parent_hwnd, 'shot': 0, 'click': 0}

    shot_spec = adapter_spec.get('shot') or {'by': 'parent'}
    shot = resolve_spec(parent_hwnd, shot_spec, ctx)
    if not shot:
        return None
    ctx['shot'] = shot

    click_spec = adapter_spec.get('click') or {'by': 'shot'}
    click = resolve_spec(parent_hwnd, click_spec, ctx)
    if not click:
        click = shot

    w, h = get_client_size(shot)
    if w <= 0 or h <= 0:
        return None

    return shot, click, w, h