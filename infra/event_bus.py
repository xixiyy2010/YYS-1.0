# infra/event_bus.py
"""
EventBus —— IEventBus 的进程内实现（线程安全）

设计要点：
    - 按事件类型订阅（type(event)），精确匹配
    - 发布时同步调用所有 handler，异常不扩散（单个 handler 抛异常不影响其他）
    - 每个事件带 sender 字段，订阅者可通过 sender 忽略自己发出的事件
      （本类不主动过滤，由订阅者决定，符合 v5.0 约定）
    - 线程安全：所有操作在 RLock 下
    - 支持 cancel handle：subscribe 返回无参函数，调用即取消订阅

v1.0 简化：
    - 不做事件优先级
    - 不做异步派发（全部同步）
    - 不做弱引用（订阅者需自行 cancel）

依赖：contracts.core
"""
from __future__ import annotations

import logging
import threading
from collections import defaultdict
from typing import Callable, Dict, List, Optional

from contracts.core import Event, IEventBus


logger = logging.getLogger(__name__)


class EventBus(IEventBus):
    """IEventBus 的进程内实现"""

    def __init__(self):
        self._lock = threading.RLock()
        # event_type → [handler, ...]
        self._handlers: Dict[type, List[Callable[[Event], None]]] = defaultdict(list)

    # ==================================================================
    #  发布
    # ==================================================================

    def publish(self, event: Event) -> None:
        if event is None:
            return

        event_type = type(event)

        with self._lock:
            handlers = list(self._handlers.get(event_type, ()))

        if not handlers:
            return

        for handler in handlers:
            try:
                handler(event)
            except Exception:
                logger.exception(
                    'EventBus: handler 抛异常 event=%s handler=%r',
                    event_type.__name__, handler)

    # ==================================================================
    #  订阅
    # ==================================================================

    def subscribe(self, event_type: type,
                  handler: Callable[[Event], None]
                  ) -> Callable[[], None]:
        if event_type is None or handler is None:
            return lambda: None

        with self._lock:
            self._handlers[event_type].append(handler)

        def _unsubscribe():
            with self._lock:
                lst = self._handlers.get(event_type)
                if not lst:
                    return
                try:
                    lst.remove(handler)
                except ValueError:
                    pass
                if not lst:
                    self._handlers.pop(event_type, None)

        return _unsubscribe

    # ==================================================================
    #  清理（调试 / 关闭时用）
    # ==================================================================

    def clear(self) -> None:
        with self._lock:
            self._handlers.clear()

    def debug_stats(self) -> dict:
        with self._lock:
            return {
                evt.__name__: len(hs)
                for evt, hs in self._handlers.items()
            }


# ==================================================================
#  全局单例（Host 创建并注入，非 Host 代码不应直接调用）
# ==================================================================

_global_bus: Optional[EventBus] = None
_global_lock = threading.Lock()


def set_event_bus(bus: Optional[EventBus]) -> None:
    """
    Host 启动时调用，把实例设为全局单例。
    传 None 表示清除（例如 Host 关闭）。
    """
    global _global_bus
    with _global_lock:
        _global_bus = bus


def get_event_bus() -> Optional[EventBus]:
    """
    获取全局单例。

    v1.0 过渡期用：某些旧模块（PopupScheduler / AutoScheduler）
    通过这个函数拿到 bus，等所有模块都改成"构造时注入"后可以删。

    正式规则（v5.0）：
        - Host 创建 bus
        - 通过构造函数 / register_resource 注入给需要的对象
        - 不允许任何模块自己 new EventBus()
    """
    return _global_bus