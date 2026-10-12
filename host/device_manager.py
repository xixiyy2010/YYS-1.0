# host/device_manager.py
"""
DeviceManager —— 设备生命周期管理

改动要点（v6）：
    1. 合并"模板获取成功"与"基准分辨率"两条日志为一行，
       基准名按高度映射（2160→4K / 1080→1K / 900→900P / 720→720P）。
    2. 订阅 WorkerCompletedEvent / WorkerPermanentlyFailedEvent，
       worker 自然结束或崩溃时清 binding.running_task_id，
       避免 sync 时向 UI 报告过期的运行任务。
    3. start_task 加 max_count 参数，透传到 boss。
    4. _create_boss 加 parent_hwnd 参数，塞进 device_context，
       供 boss 传给 WorkerLauncher.launch（worker_entry 里
       set_device_hwnd 语义上应存 parent_hwnd，而不是 shot_hwnd）。

原有：
    - 保留 v2 里对 device_utils 的全部写操作
      （set_device_hwnd / set_device_render_hwnd / set_window_size /
       sync_base_resolution_from_index / register_shared_memory）
    - 保留 v4 里的 ToolKit 收敛字段 / shm_registered 标记
    - 保留 bind/unbind 里的 _cleanup_device_utils

顺序：
    设 templates → 解析句柄 → 登记句柄/尺寸/基准
    → 起截图子进程 → 注册 shm → 起老板

中立原则：
    引擎不认识"游戏 / 模拟器"。字段名中性：
        app_id / target_title / adapter_key
    座位名中性：business / bind / alert / switch_1 / ...

依赖：
    capability.hwnd / capability.resource_paths / capability.screenshot
    capability.device_utils
    contracts.* / infra.* / host.worker_launcher
"""
from __future__ import annotations

import importlib
import json
import logging
import os
import threading
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from capability import hwnd as hwnd_util
from capability import resource_paths
from capability import device_utils
from capability.screenshot import ScreenshotWorker
from contracts.boss import BossReporter, TaskSpec, ToolKit
from contracts.core import IEventBus
from contracts.host import (
    DeviceConnectedEvent, DeviceDisconnectedEvent,
    BossStateChangedEvent, SlotChangedEvent,
    WorkerPermanentlyFailedEvent,
)
from contracts.worker import WorkerCompletedEvent
from infra.ipc_bridge import IPCServer
from infra.log_manager import LogManager
from host.worker_launcher import WorkerLauncher


logger = logging.getLogger(__name__)


# ==================================================================
#  基准名映射（日志用）
# ==================================================================

def _format_base_name(w: int, h: int) -> str:
    """把基准分辨率转成简称：2160→4K，1440→2K，1080→1K，900→900P..."""
    try:
        height = int(h)
    except Exception:
        return f'{w}x{h}'
    mapping = {
        2160: '4K',
        1440: '2K',
        1080: '1K',
        900:  '900P',
        720:  '720P',
        576:  '576P',
        480:  '480P',
    }
    return mapping.get(height, f'{height}P')


class _DeviceBossReporter(BossReporter):
    """把 Boss 的上报翻译成 EventBus 事件 + LogManager 日志"""

    def __init__(self, device: str, event_bus: IEventBus,
                 log_manager: LogManager):
        self._device = device
        self._bus = event_bus
        self._log = log_manager

    def state(self, state: str, detail: str = '') -> None:
        try:
            self._bus.publish(BossStateChangedEvent(
                sender='Boss',
                device=self._device,
                state=state,
                detail=detail,
            ))
        except Exception:
            pass

    def log(self, message: str, level: str = 'info',
            ui_message: str = None,
            quiet: bool = False) -> None:
        if level == 'error':
            self._log.error(self._device, message,
                            ui_message=ui_message, quiet=quiet)
        elif level == 'warning':
            self._log.warning(self._device, message,
                              ui_message=ui_message, quiet=quiet)
        else:
            self._log.info(self._device, message,
                           ui_message=ui_message, quiet=quiet)

    def slot(self, slot_name: str, value) -> None:
        try:
            self._bus.publish(SlotChangedEvent(
                sender='Boss',
                device=self._device,
                slot_name=slot_name,
                value=value,
            ))
        except Exception:
            pass


@dataclass
class DeviceBinding:
    device: str
    target_title: str
    adapter_key: str
    app_id: str
    parent_hwnd: int
    click_hwnd: int
    shot_hwnd: int
    window_w: int
    window_h: int
    templates_dir: str = ''
    screenshot_worker: Optional[ScreenshotWorker] = None
    boss: Any = None
    reporter: Optional[BossReporter] = None
    running_task_id: str = ''
    shm_registered: bool = False


class DeviceManager:

    def __init__(self, event_bus: IEventBus, ipc_server: IPCServer,
                 log_manager: LogManager,
                 base_dir: Optional[str] = None):
        self._bus = event_bus
        self._ipc = ipc_server
        self._log = log_manager

        if base_dir is None:
            base_dir = os.path.dirname(
                os.path.dirname(os.path.abspath(__file__)))
        self._base_dir = base_dir

        self._lock = threading.RLock()
        self._devices: Dict[str, DeviceBinding] = {}
        self._binding_in_progress: set = set()

        self._launcher = WorkerLauncher(event_bus, ipc_server)

        # 订阅 worker 完成 / 崩溃事件，清理 binding.running_task_id
        self._unsub_worker_done = self._bus.subscribe(
            WorkerCompletedEvent, self._on_worker_finished)
        self._unsub_worker_fail = self._bus.subscribe(
            WorkerPermanentlyFailedEvent, self._on_worker_finished)

    # ==================================================================
    #  名片读取
    # ==================================================================

    def _read_config(self, app_id: str) -> Optional[dict]:
        path = os.path.join(
            self._base_dir, 'plugins', app_id, 'config.json')
        if not os.path.isfile(path):
            self._log.error('', f'找不到名片: {path}')
            return None
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            self._log.error('', f'读名片失败 {path}: {e}')
            return None

    def _resolve_templates_dir(self, cfg: dict) -> str:
        resources = cfg.get('resources', {}) or {}
        rel = resources.get('templates_dir', '') or ''
        if not rel:
            return ''
        if os.path.isabs(rel):
            abs_path = rel
        else:
            abs_path = os.path.normpath(
                os.path.join(self._base_dir, rel))
        return abs_path

    def _find_adapter_rule(self, cfg: dict, target_title: str
                           ) -> Tuple[Optional[str], Optional[dict]]:
        slots = cfg.get('slots', {}) or {}
        bind = slots.get('bind', {}) or {}
        rules = bind.get('rules') or []

        for rule in rules:
            keyword = rule.get('window_keyword', '')
            if keyword and target_title.startswith(keyword):
                return keyword, rule
        return None, None

    # ==================================================================
    #  候选列表
    # ==================================================================

    def list_candidates(self, app_id: str) -> List[Tuple[str, str]]:
        cfg = self._read_config(app_id)
        if cfg is None:
            return []

        slots = cfg.get('slots', {}) or {}
        bind = slots.get('bind', {}) or {}
        rules = bind.get('rules') or []

        if not rules:
            self._log.error('', f'名片 {app_id} 没有声明 bind.rules')
            return []

        results: List[Tuple[str, str]] = []
        try:
            top_windows = hwnd_util.enum_top_windows()
        except Exception as e:
            self._log.error('', f'枚举顶层窗口异常: {e}')
            return results

        for _hwnd, title in top_windows:
            for rule in rules:
                keyword = rule.get('window_keyword', '')
                if keyword and title.startswith(keyword):
                    results.append((title, keyword))
                    break
        return results

    # ==================================================================
    #  绑定
    # ==================================================================

    def bind(self, target_title: str, app_id: str) -> bool:
        device = target_title

        with self._lock:
            if device in self._devices or device in self._binding_in_progress:
                self._log.warning(device, '正在绑定或已绑定，忽略')
                return False
            self._binding_in_progress.add(device)

        try:
            cfg = self._read_config(app_id)
            if cfg is None:
                return False

            parent_hwnd = hwnd_util.find_top_window_by_title(target_title)
            if not parent_hwnd:
                self._log.error(device, f'未找到窗口 {target_title}')
                return False

            adapter_key, rule = self._find_adapter_rule(cfg, target_title)
            if rule is None:
                self._log.error(device, '无匹配规则')
                return False

            # ---------- 先设 templates 目录 ----------
            templates_dir = self._resolve_templates_dir(cfg)
            if templates_dir:
                if not resource_paths.set_templates_dir(templates_dir):
                    self._log.warning(
                        device, f'templates 目录无效: {templates_dir}')
            else:
                self._log.warning(
                    device, '名片没有声明 resources.templates_dir')

            # ---------- 解析句柄 ----------
            adapter_spec = {
                'shot': rule.get('shot') or {'by': 'parent'},
                'click': rule.get('click') or {'by': 'shot'},
            }
            resolved = hwnd_util.resolve_handles(parent_hwnd, adapter_spec)
            if resolved is None:
                self._log.error(device, '解析句柄失败')
                return False
            shot_hwnd, click_hwnd, w, h = resolved

            # ---------- 登记句柄 / 尺寸 / 基准 ----------
            device_utils.set_device_hwnd(device, parent_hwnd)
            device_utils.set_device_render_hwnd(device, click_hwnd)
            device_utils.set_window_size(device, w, h)

            try:
                if not device_utils.sync_base_resolution_from_index(
                        device=device):
                    self._log.warning(device, '未找到基准配置文件')
            except Exception as e:
                self._log.error(device, f'基准分辨率同步异常: {e}')

            # ---------- 起截图子进程 ----------
            shot_worker = ScreenshotWorker(shot_hwnd, self._base_dir)
            if not shot_worker.start():
                self._log.error(device, '截图子进程启动失败')
                self._cleanup_device_utils(device)
                return False

            # ---------- 注册共享内存 ----------
            shm_registered = False
            try:
                device_utils.register_shared_memory(
                    device, shot_worker.shm_name)
                shm_registered = True
            except Exception as e:
                self._log.error(device, f'共享内存注册失败: {e}')
                shot_worker.stop()
                self._cleanup_device_utils(device)
                return False

            # ---------- 起老板 ----------
            reporter = _DeviceBossReporter(device, self._bus, self._log)
            try:
                boss = self._create_boss(
                    device, app_id, cfg, reporter,
                    parent_hwnd, click_hwnd, shot_hwnd,
                    shot_worker.shm_name,
                    w, h, templates_dir)
            except Exception as e:
                self._log.error(device, f'创建老板失败: {e}')
                if shm_registered:
                    try:
                        device_utils.unregister_shared_memory(device)
                    except Exception:
                        pass
                shot_worker.stop()
                self._cleanup_device_utils(device)
                return False

            binding = DeviceBinding(
                device=device,
                target_title=target_title,
                adapter_key=adapter_key or '',
                app_id=app_id,
                parent_hwnd=parent_hwnd,
                click_hwnd=click_hwnd,
                shot_hwnd=shot_hwnd,
                window_w=w,
                window_h=h,
                templates_dir=templates_dir,
                screenshot_worker=shot_worker,
                boss=boss,
                reporter=reporter,
                shm_registered=shm_registered,
            )
            with self._lock:
                self._devices[device] = binding

            self._log.info(device, f'✅ 连接设备: {device}')
            self._log.info(device, f'✅ 绑定窗口: {target_title}')
            self._log.info(device, f'✅ 已绑定点击窗口: {click_hwnd}')
            self._log.info(device, f'✅ 已找到渲染窗口: {shot_hwnd}')
            self._log.info(device, f'✅ 截图窗口尺寸: {w}x{h}')

            try:
                res = device_utils.get_active_base_resolution(device=device)
            except Exception:
                res = None
            if templates_dir:
                if res:
                    base_name = _format_base_name(res[0], res[1])
                    self._log.info(
                        device, f'✅ 模板获取成功: 基准{base_name}')
                else:
                    self._log.info(device, '✅ 模板获取成功')
            self._log.info(device, '✅ PrintWindow截图已启用')

            try:
                self._bus.publish(DeviceConnectedEvent(
                    sender='DeviceManager',
                    device=device,
                    window_title=target_title,
                    emulator_type=adapter_key or '',
                    click_hwnd=click_hwnd,
                    shot_hwnd=shot_hwnd,
                    window_w=w,
                    window_h=h,
                ))
            except Exception as e:
                self._log.error(
                    device, f'发布 DeviceConnectedEvent 异常: {e}')

            try:
                if hasattr(boss, 'report_task_list'):
                    boss.report_task_list()
            except Exception as e:
                self._log.error(device, f'report_task_list 异常: {e}')
            try:
                if hasattr(boss, 'report_slot_state'):
                    boss.report_slot_state()
            except Exception as e:
                self._log.error(device, f'report_slot_state 异常: {e}')

            return True

        finally:
            with self._lock:
                self._binding_in_progress.discard(device)

    def _create_boss(self, device: str, app_id: str,
                     cfg: dict, reporter: BossReporter,
                     parent_hwnd: int,
                     click_hwnd: int, shot_hwnd: int, shm_name: str,
                     window_w: int, window_h: int,
                     templates_dir: str = ''):
        boss_path = cfg.get('boss', '')
        if '.' not in boss_path:
            raise ValueError(f'config.json 里 boss 字段无效: {boss_path}')
        module_path, class_name = boss_path.rsplit('.', 1)
        module = importlib.import_module(module_path)
        boss_cls = getattr(module, class_name)

        tasks = []
        for t in cfg.get('tasks', []) or []:
            tasks.append(TaskSpec(
                task_id=t.get('id', ''),
                name=t.get('name', ''),
                class_name=t.get('class', ''),
                module_file=t.get('module_file', ''),
                folder=t.get('folder', ''),
                stats=list(t.get('stats', []) or []),
                default_count=t.get('default_count', 'inf'),
            ))

        tools = ToolKit(
            base_dir=self._base_dir,
            game_folder=app_id,
            templates_dir=templates_dir,
            device_context={
                'parent_hwnd': parent_hwnd,
                'click_hwnd': click_hwnd,
                'shot_hwnd': shot_hwnd,
                'shm_name': shm_name,
                'window_w': window_w,
                'window_h': window_h,
                'templates_dir': templates_dir,
            },
        )

        slots = cfg.get('slots', {}) or {}

        boss = boss_cls(
            device=device,
            tools=tools,
            reporter=reporter,
            tasks=tasks,
            event_bus=self._bus,
            worker_launcher=self._launcher,
            slots=slots,
        )
        return boss

    # ==================================================================
    #  座位操作（UI → 老板）
    # ==================================================================

    def slot_command(self, device: str, slot_name: str, value) -> bool:
        with self._lock:
            binding = self._devices.get(device)
        if binding is None or binding.boss is None:
            self._log.warning(device, f'座位操作失败: 未绑定 device={device}')
            return False
        try:
            binding.boss.on_slot_command(slot_name, value)
            return True
        except Exception as e:
            self._log.error(device, f'座位操作异常 slot={slot_name}: {e}')
            return False

    # ==================================================================
    #  激活窗口（UI → 引擎）
    # ==================================================================

    def activate_window(self, device: str) -> bool:
        with self._lock:
            binding = self._devices.get(device)
        if binding is None:
            return False
        try:
            return hwnd_util.activate_window(binding.parent_hwnd)
        except Exception as e:
            self._log.error(device, f'激活窗口异常: {e}')
            return False

    # ==================================================================
    #  解绑
    # ==================================================================

    def unbind(self, device: str) -> None:
        with self._lock:
            binding = self._devices.pop(device, None)
        if binding is None:
            return

        if binding.boss is not None:
            try:
                binding.boss.shutdown()
            except Exception as e:
                self._log.error(device, f'老板 shutdown 异常: {e}')

        try:
            self._launcher.terminate(device, reason='device unbind')
        except Exception as e:
            self._log.error(device, f'停 Worker 异常: {e}')

        if getattr(binding, 'shm_registered', False):
            try:
                device_utils.unregister_shared_memory(device)
            except Exception as e:
                self._log.error(device, f'共享内存注销异常: {e}')

        if binding.screenshot_worker is not None:
            try:
                binding.screenshot_worker.stop()
            except Exception as e:
                self._log.error(device, f'停截图子进程异常: {e}')

        self._cleanup_device_utils(device)

        try:
            self._bus.publish(DeviceDisconnectedEvent(
                sender='DeviceManager', device=device))
        except Exception as e:
            self._log.error(device, f'发布 DeviceDisconnectedEvent 异常: {e}')

        self._log.info(device, '⏹️ 设备已断开')

    def _cleanup_device_utils(self, device: str) -> None:
        """清掉本进程 device_utils 里这台设备的句柄登记（幂等）"""
        try:
            device_utils.set_device_render_hwnd(device, None)
            device_utils.set_device_hwnd(device, None)
        except Exception:
            pass

    # ==================================================================
    #  任务启停
    # ==================================================================

    def start_task(self, device: str, task_id: str,
                   max_count: str = 'inf') -> bool:
        with self._lock:
            binding = self._devices.get(device)
        if binding is None or binding.boss is None:
            self._log.error(device, '未绑定，无法启动任务')
            return False

        ok = binding.boss.start_task(task_id, max_count=max_count)
        if ok:
            binding.running_task_id = task_id
            device_utils.set_shot_mode(device, 30)   # ← 切快模式
        return ok

    def stop_task(self, device: str) -> None:
        with self._lock:
            binding = self._devices.get(device)
        if binding is None or binding.boss is None:
            return
        try:
            binding.boss.stop_task()
        except Exception as e:
            self._log.error(device, f'停任务异常: {e}')
        binding.running_task_id = ''
        device_utils.set_shot_mode(device, 100)      # ← 切回空闲

    def is_task_running(self, device: str) -> bool:
        with self._lock:
            binding = self._devices.get(device)
        if binding is None:
            return False
        return bool(binding.running_task_id)

    def _on_worker_finished(self, evt) -> None:
        """
        worker 自然完成 / 崩溃 → 清 binding.running_task_id。

        事件的 worker_id / sender 都对应 device（launch 时
        worker_id=self._device），用哪个都行，兼容取。
        """
        wid = (getattr(evt, 'worker_id', '')
               or getattr(evt, 'sender', '')
               or getattr(evt, 'device', ''))
        if not wid:
            return
        with self._lock:
            binding = self._devices.get(wid)
        if binding is None:
            return
        if binding.running_task_id:
            binding.running_task_id = ''
        device_utils.set_shot_mode(wid, 100)         # ← 切回空闲

    # ==================================================================
    #  查询
    # ==================================================================

    def list_devices(self) -> List[str]:
        with self._lock:
            return list(self._devices.keys())

    def get_binding(self, device: str) -> Optional[DeviceBinding]:
        with self._lock:
            return self._devices.get(device)

    def get_boss(self, device: str):
        with self._lock:
            binding = self._devices.get(device)
        return binding.boss if binding else None

    # ==================================================================
    #  关闭
    # ==================================================================

    def shutdown(self) -> None:
        with self._lock:
            devices = list(self._devices.keys())
        for d in devices:
            self.unbind(d)

        # 取消 worker 事件订阅
        if getattr(self, '_unsub_worker_done', None):
            try:
                self._unsub_worker_done()
            except Exception:
                pass
            self._unsub_worker_done = None
        if getattr(self, '_unsub_worker_fail', None):
            try:
                self._unsub_worker_fail()
            except Exception:
                pass
            self._unsub_worker_fail = None

        try:
            self._launcher.shutdown()
        except Exception as e:
            self._log.error('', f'WorkerLauncher shutdown 异常: {e}')