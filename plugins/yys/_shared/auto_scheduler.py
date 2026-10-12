# plugins/yys/_shared/auto_scheduler.py
"""
AutoScheduler —— 老板的内部器官（调度决策）

职责：
    1. 只订阅并处理【本老板绑定的设备】的状态事件
    2. 根据 STATE_PAUSE_MAP 计算本设备当前应暂停的资源
    3. 状态变化时，通过 EventBus 发布 PauseResourceEvent / ResumeResourceEvent
    4. 不直接调用任何其他服务

每个老板一个 AutoScheduler。设备隔离。

依赖：contracts.core / contracts.host / plugins.yys._shared.resources
"""
from __future__ import annotations

import logging
import threading
from typing import Set

from contracts.core import (
    IEventBus,
    StateEnteredEvent, StateExitedEvent,
)
from contracts.host import PauseResourceEvent, ResumeResourceEvent
from plugins.yys._shared.resources import STATE_PAUSE_MAP


logger = logging.getLogger(__name__)


class AutoScheduler:
    """老板的调度器官（每老板一个，只认一个设备）"""

    def __init__(self, event_bus: IEventBus, device: str):
        self._bus = event_bus
        self._device = device
        self._lock = threading.RLock()

        # 该设备当前所处的状态集合
        self._states: Set[str] = set()

        # 当前已判定为"应暂停"的资源集合
        self._paused: Set[str] = set()

        # 订阅状态事件
        self._unsub_entered = self._bus.subscribe(
            StateEnteredEvent, self._on_state_entered)
        self._unsub_exited = self._bus.subscribe(
            StateExitedEvent, self._on_state_exited)

    # ==================================================================
    #  事件处理
    # ==================================================================

    def _match_device(self, evt) -> bool:
        """兼容 sender / device 两种字段名：命中任一即视为本设备事件。"""
        device = getattr(evt, 'device', None) or getattr(evt, 'sender', None)
        return device == self._device

    def _on_state_entered(self, evt: StateEnteredEvent) -> None:
        if not self._match_device(evt):
            return
        if not evt.state_name:
            return
        with self._lock:
            if evt.state_name in self._states:
                return
            self._states.add(evt.state_name)
            logger.info(
                'AutoScheduler[%s]: 进入状态 %s',
                self._device, evt.state_name)
            self._apply_locked()

    def _on_state_exited(self, evt: StateExitedEvent) -> None:
        if not self._match_device(evt):
            return
        if not evt.state_name:
            return
        with self._lock:
            if evt.state_name not in self._states:
                return
            self._states.discard(evt.state_name)
            logger.info(
                'AutoScheduler[%s]: 退出状态 %s',
                self._device, evt.state_name)
            self._apply_locked()

    # ==================================================================
    #  决策
    # ==================================================================

    def _apply_locked(self) -> None:
        """重新计算该设备应暂停的资源，只对变化部分发布事件。"""
        should_pause: Set[str] = set()
        for state_name in self._states:
            for resource_name in STATE_PAUSE_MAP.get(state_name, []):
                should_pause.add(resource_name)

        newly_paused = should_pause - self._paused
        newly_resumed = self._paused - should_pause

        self._paused = should_pause

        for resource_name in newly_paused:
            logger.info(
                'AutoScheduler[%s]: 发布暂停 %s',
                self._device, resource_name)
            self._bus.publish(PauseResourceEvent(
                sender='AutoScheduler',
                device=self._device,
                resource_name=resource_name,
            ))

        for resource_name in newly_resumed:
            logger.info(
                'AutoScheduler[%s]: 发布恢复 %s',
                self._device, resource_name)
            self._bus.publish(ResumeResourceEvent(
                sender='AutoScheduler',
                device=self._device,
                resource_name=resource_name,
            ))

    # ==================================================================
    #  查询 / 关闭
    # ==================================================================

    def get_status(self) -> dict:
        with self._lock:
            return {
                'device': self._device,
                'states': list(self._states),
                'paused': list(self._paused),
            }

    def shutdown(self) -> None:
        # 锁内取走引用并置空，避免与事件回调并发
        with self._lock:
            unsub_entered = self._unsub_entered
            unsub_exited = self._unsub_exited
            self._unsub_entered = None
            self._unsub_exited = None

        # 锁外执行回调，防止回调内部再进同一把锁造成死锁
        for unsub in (unsub_entered, unsub_exited):
            if unsub is None:
                continue
            try:
                unsub()
            except Exception:
                pass