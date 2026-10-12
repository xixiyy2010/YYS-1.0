# host/engine_service.py
"""
EngineService —— 引擎侧的 UI 服务层

改动要点（v4）：
    _cmd_shutdown 不再提前 set _stop_event。
    stop() 第一行是 if self._stop_event.is_set(): return，
    提前 set 会让 stop() 变成 no-op，
    _unsubscribe_all / _ui_host.stop 全都不会执行。
    现改为起线程直接调 stop()，由 stop() 自己 set。

职责：
    1. 持有 UIHost（引擎侧的命名管道监听器）
    2. 订阅 EventBus 上的业务事件，转成 UI 事件推送
    3. 接收 UI 命令，分发给 DeviceManager

模型：
    - UI → 引擎：命令（sync / bind / start_task / slot_command / ...），引擎同步执行
    - 引擎 → UI：事件（device_connected / boss_state / slot_changed / log / ...），单向推送
    - "sync" 是唯一的一次性全量快照，用于 UI 冷启动时画界面

中立原则：
    本文件不认识"游戏 / 模拟器"。字段名中性：
        app_id / target_title / adapter_key
    座位信号原样搬运：
        slot_name（"alert" / "switch_1" / ...）和 value 都不解析

依赖：
    contracts.ui_protocol
    contracts.core / contracts.host
    infra.ui_bridge (UIHost)
    infra.log_manager (LogManager)
    host.device_manager / host.plugin_loader
"""
from __future__ import annotations

import logging
import threading
from typing import Optional

from contracts.core import IEventBus
from contracts.host import (
    DeviceConnectedEvent, DeviceDisconnectedEvent,
    BossStateChangedEvent, SlotChangedEvent,
)
from contracts.ui_protocol import (
    UICommands, UIEvents,
)
from infra.ui_bridge import UIHost
from infra.log_manager import LogManager
from host.device_manager import DeviceManager
from host.plugin_loader import PluginLoader


logger = logging.getLogger(__name__)


class EngineService:
    """引擎侧的 UI 服务层（一个引擎进程一个实例）"""

    def __init__(self,
                 event_bus: IEventBus,
                 log_manager: LogManager,
                 device_manager: DeviceManager,
                 plugin_loader: PluginLoader):
        self._bus = event_bus
        self._log = log_manager
        self._dm = device_manager
        self._plugin_loader = plugin_loader

        self._ui_host = UIHost()
        self._stop_event = threading.Event()

        self._unsubs = []
        self._log_listener = None

        self._boss_states: dict = {}
        self._boss_details: dict = {}

    # ==================================================================
    #  生命周期
    # ==================================================================

    def start(self) -> bool:
        self._ui_host.on_command(self._on_ui_command)
        self._ui_host.on_connect(self._on_ui_connect)
        self._ui_host.on_disconnect(self._on_ui_disconnect)

        self._unsubs.append(self._bus.subscribe(
            DeviceConnectedEvent, self._on_device_connected))
        self._unsubs.append(self._bus.subscribe(
            DeviceDisconnectedEvent, self._on_device_disconnected))
        self._unsubs.append(self._bus.subscribe(
            BossStateChangedEvent, self._on_boss_state))
        self._unsubs.append(self._bus.subscribe(
            SlotChangedEvent, self._on_slot_changed))

        self._log_listener = self._on_log
        self._log.add_listener(self._log_listener)

        if not self._ui_host.start():
            logger.error('EngineService: UIHost 启动失败')
            self._unsubscribe_all()
            return False

        logger.info('EngineService: 已启动')
        return True

    def stop(self) -> None:
        if self._stop_event.is_set():
            return
        self._stop_event.set()
        self._unsubscribe_all()
        try:
            self._ui_host.stop()
        except Exception:
            logger.exception('EngineService: UIHost.stop 异常')
        logger.info('EngineService: 已停止')

    def wait(self, timeout: Optional[float] = None) -> bool:
        return self._stop_event.wait(timeout)

    def _unsubscribe_all(self) -> None:
        for unsub in self._unsubs:
            try:
                unsub()
            except Exception:
                pass
        self._unsubs.clear()

        if self._log_listener is not None:
            try:
                self._log.remove_listener(self._log_listener)
            except Exception:
                pass
            self._log_listener = None

    # ==================================================================
    #  UI 侧连接回调
    # ==================================================================

    def _on_ui_connect(self) -> None:
        logger.info('EngineService: UI 已连接')

    def _on_ui_disconnect(self) -> None:
        logger.info('EngineService: UI 已断开')

    # ==================================================================
    #  UI 命令分发
    # ==================================================================

    def _on_ui_command(self, name: str, data: dict) -> None:
        handlers = {
            UICommands.SYNC:            self._cmd_sync,
            UICommands.LIST_CANDIDATES: self._cmd_list_candidates,
            UICommands.LIST_APPS:       self._cmd_list_apps,
            UICommands.BIND:            self._cmd_bind,
            UICommands.UNBIND:          self._cmd_unbind,
            UICommands.START_TASK:      self._cmd_start_task,
            UICommands.STOP_TASK:       self._cmd_stop_task,
            UICommands.SLOT_COMMAND:    self._cmd_slot_command,
            UICommands.ACTIVATE_WINDOW: self._cmd_activate_window,
            UICommands.SHUTDOWN:        self._cmd_shutdown,
        }
        handler = handlers.get(name)
        if handler is None:
            logger.warning('EngineService: 未知命令 %s', name)
            return
        try:
            handler(data or {})
        except Exception:
            logger.exception('EngineService: 命令处理异常 name=%s', name)

    # ==================================================================
    #  命令实现
    # ==================================================================

    def _cmd_sync(self, data: dict) -> None:
        payload = {
            'apps': self._collect_apps(),
            'devices': self._collect_devices(),
        }
        self._ui_host.send_event(UIEvents.SYNC_RESULT, payload)

    def _cmd_list_apps(self, data: dict) -> None:
        """下拉框展开时，现扫一遍业务列表，推给 UI"""
        apps = self._collect_apps()      # 复用现有扫描逻辑
        self._ui_host.send_event(UIEvents.APPS_UPDATED, {'apps': apps})

    def _cmd_list_candidates(self, data: dict) -> None:
        app_id = data.get('app_id', '') or ''
        try:
            raw = self._dm.list_candidates(app_id)
        except Exception:
            logger.exception(
                'EngineService: list_candidates 异常 app_id=%s', app_id)
            raw = []

        candidates = []
        for item in raw:
            if isinstance(item, (tuple, list)) and len(item) >= 2:
                candidates.append({
                    'target_title': item[0],
                    'adapter_key': item[1],
                })
            elif isinstance(item, str):
                candidates.append({
                    'target_title': item,
                    'adapter_key': '',
                })

        self._ui_host.send_event(UIEvents.CANDIDATES_RESULT, {
            'app_id': app_id,
            'candidates': candidates,
        })

    def _cmd_bind(self, data: dict) -> None:
        target_title = data.get('target_title', '') or ''
        app_id = data.get('app_id', '') or ''
        if not target_title or not app_id:
            logger.warning('EngineService: bind 参数缺失')
            return
        self._dm.bind(target_title, app_id)

    def _cmd_unbind(self, data: dict) -> None:
        device = data.get('device', '') or ''
        if not device:
            return
        self._dm.unbind(device)

    def _cmd_start_task(self, data: dict) -> None:
        device = data.get('device', '') or ''
        task_id = data.get('task_id', '') or ''
        max_count = data.get('max_count', 'inf') or 'inf'
        if not device or not task_id:
            return
        self._dm.start_task(device, task_id, max_count=max_count)

    def _cmd_stop_task(self, data: dict) -> None:
        device = data.get('device', '') or ''
        if not device:
            return
        self._dm.stop_task(device)

    def _cmd_slot_command(self, data: dict) -> None:
        device = data.get('device', '') or ''
        slot_name = data.get('slot', '') or ''
        value = data.get('value')
        if not device or not slot_name:
            return
        self._dm.slot_command(device, slot_name, value)

    def _cmd_activate_window(self, data: dict) -> None:
        device = data.get('device', '') or ''
        if not device:
            return
        self._dm.activate_window(device)

    def _cmd_shutdown(self, data: dict) -> None:
        logger.info('EngineService: 收到 shutdown 命令')
        # 不要在这里 set _stop_event：
        # stop() 第一行就是 if self._stop_event.is_set(): return，
        # 提前 set 会让 stop() 直接 no-op，
        # _unsubscribe_all / _ui_host.stop 全都不会执行。
        # 交给 stop() 自己 set。
        threading.Thread(
            target=self._safe_stop, name='EngineService-Stop',
            daemon=True).start()

    def _safe_stop(self) -> None:
        try:
            self.stop()
        except Exception:
            logger.exception('EngineService: 异步 stop 异常')

    # ==================================================================
    #  快照收集
    # ==================================================================

    def _collect_apps(self) -> list:
        apps = []
        try:
            app_ids = self._plugin_loader.list_apps()
        except Exception:
            logger.exception('EngineService: list_apps 异常')
            return apps

        for app_id in app_ids:
            try:
                cfg = self._plugin_loader.read_config(app_id)
            except Exception:
                cfg = None
            if cfg is None:
                continue

            display_name = cfg.get('name', app_id) or app_id
            slots = cfg.get('slots', {}) or {}

            tasks = []
            for t in cfg.get('tasks', []) or []:
                tid = t.get('id', '')
                if not tid:
                    continue
                tasks.append({
                    'task_id': tid,
                    'name': t.get('name', tid),
                    'stats': list(t.get('stats', []) or []),
                    'default_count': t.get('default_count', 'inf'),
                })

            apps.append({
                'id': app_id,
                'name': display_name,
                'slots': slots,
                'tasks': tasks,
            })
        return apps

    def _collect_devices(self) -> list:
        devices = []
        try:
            device_list = self._dm.list_devices()
        except Exception:
            logger.exception('EngineService: list_devices 异常')
            return devices

        for device in device_list:
            try:
                binding = self._dm.get_binding(device)
            except Exception:
                binding = None
            if binding is None:
                continue

            devices.append({
                'device': device,
                'target_title': binding.target_title,
                'adapter_key': binding.adapter_key,
                'app_id': binding.app_id,
                'state': self._boss_states.get(device, 'idle'),
                'detail': self._boss_details.get(device, ''),
                'running_task_id': binding.running_task_id or '',
                'window_w': binding.window_w,
                'window_h': binding.window_h,
            })
        return devices

    # ==================================================================
    #  EventBus 事件 → UI 事件
    # ==================================================================

    def _on_device_connected(self, evt: DeviceConnectedEvent) -> None:
        try:
            binding = self._dm.get_binding(evt.device)
        except Exception:
            binding = None
        app_id = binding.app_id if binding else ''

        self._ui_host.send_event(UIEvents.DEVICE_CONNECTED, {
            'device': evt.device,
            'target_title': evt.window_title,
            'adapter_key': evt.emulator_type,
            'app_id': app_id,
            'window_w': evt.window_w,
            'window_h': evt.window_h,
        })

    def _on_device_disconnected(self, evt: DeviceDisconnectedEvent) -> None:
        self._boss_states.pop(evt.device, None)
        self._boss_details.pop(evt.device, None)
        self._ui_host.send_event(UIEvents.DEVICE_DISCONNECTED, {
            'device': evt.device,
        })

    def _on_boss_state(self, evt: BossStateChangedEvent) -> None:
        self._boss_states[evt.device] = evt.state
        self._boss_details[evt.device] = evt.detail
        self._ui_host.send_event(UIEvents.BOSS_STATE, {
            'device': evt.device,
            'state': evt.state,
            'detail': evt.detail,
        })

    def _on_slot_changed(self, evt: SlotChangedEvent) -> None:
        self._ui_host.send_event(UIEvents.SLOT_CHANGED, {
            'device': evt.device,
            'slot': evt.slot_name,
            'value': evt.value,
        })

    def _on_log(self, device: str, level: str, message: str) -> None:
        self._ui_host.send_event(UIEvents.LOG, {
            'device': device or '',
            'level': level or 'info',
            'message': message or '',
        })