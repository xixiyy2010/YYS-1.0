# plugins/yys/boss.py
"""
阴阳师业务老板。

组成：
    - 任务管理（起/停任务）
    - 两个常驻后台线程：
        XuanshangWorker —— 悬赏协助（不受开关控制）
        JixuWorker      —— 跳过结算（受 switch_1 控制）

座位机制：
    - switch_1 控制 JixuWorker.set_enabled
    - switch_2 空壳（掉线处理，暂未实现）
    - XuanshangWorker 检测到悬赏 → on_slot('alert', 'blink')
    - 绑定完成后 report_slot_state() 发 status，让悬浮窗显示初始状态

改动要点（v3）：
    - __init__ 从 device_utils 拿本设备的 base_res，传给 JixuWorker /
      XuanshangWorker，让 TemplateManager 与 device_utils.tap 用同一基准，
      避免多基准时坐标缩放错位。
    - __init__ 从 slots.switches[0].default 初始化 _jixu_worker.enabled，
      使 config.json 的声明与引擎实际状态保持一致。
    - start_task 加 max_count 参数（UI 上"次数"输入框的值），
      透传到 WorkerLauncher.launch。
    - start_task 里 launch 时透传 hwnd_parent，
      worker_entry 用它 set_device_hwnd。
改动要点（v4）：
    - Navigator 显式传入 base_res，与 device_utils.tap 用同一基准。
    - stop_task 重构：先清 _running_task_id 再出锁 terminate，
      避免 terminate 期间 _on_worker_completed 重复处理。
      terminate 出锁、_set_state(IDLE) 放最后，不阻塞事件循环。
"""
from __future__ import annotations

import logging
import threading
from typing import Dict, List, Optional

from capability.device_utils import get_active_base_resolution
from contracts.boss import IBoss, BossReporter, BossState, TaskSpec, ToolKit
from contracts.worker import (
    WorkerLogEvent, WorkerCompletedEvent, WorkerHealthEvent,
)
from contracts.host import WorkerPermanentlyFailedEvent

from plugins.yys._shared import (
    AutoScheduler,
    Navigator,
    XuanshangWorker,
    JixuWorker,
)


logger = logging.getLogger(__name__)


class _ReporterAsLogManager:
    """把 BossReporter 适配成 LogManager 接口，给后台线程用。"""

    def __init__(self, reporter, device):
        self._r = reporter
        self._d = device

    def info(self, device, message, ui_message=None, quiet=False):
        self._r.log(message, 'info',
                    ui_message=ui_message, quiet=quiet)

    def warning(self, device, message, ui_message=None, quiet=False):
        self._r.log(message, 'warning',
                    ui_message=ui_message, quiet=quiet)

    def error(self, device, message, ui_message=None, quiet=False):
        self._r.log(message, 'error',
                    ui_message=ui_message, quiet=quiet)


class YysBoss(IBoss):
    """阴阳师业务老板"""

    _START_TIMEOUT = 5.0    # 从 launch 到收到第一个心跳的最长等待

    def __init__(self,
                 device: str,
                 tools: ToolKit,
                 reporter: BossReporter,
                 tasks: List[TaskSpec],
                 event_bus,
                 worker_launcher=None,
                 slots: Optional[dict] = None):
        self._device = device
        self._tools = tools
        self._reporter = reporter
        self._tasks = {t.task_id: t for t in tasks}
        self._event_bus = event_bus
        self._worker_launcher = worker_launcher
        self._slots = slots or {}

        self._game_folder = getattr(tools, 'game_folder', '') or ''
        self._templates_dir = getattr(tools, 'templates_dir', '') or ''

        self._lock = threading.RLock()

        self._state: BossState = BossState.IDLE
        self._state_detail: str = ''
        self._running_task_id: str = ''
        self._pending_start = None
        self._stop_event = threading.Event()

        self._switch_values: Dict[str, bool] = {}

        log_adapter = _ReporterAsLogManager(reporter, device)

        # 拿本设备的当前基准分辨率（device_manager.bind 里已 set）
        # 让 Navigator / 两个 worker 的 TemplateManager 与 device_utils.tap 对齐
        try:
            base_res = get_active_base_resolution(device)
        except Exception:
            base_res = None

        self._navigator = Navigator(
            device,
            log_callback=lambda msg: self._reporter.log(msg, 'info'),
            stop_check_callback=lambda: self._stop_event.is_set(),
            target_res=base_res,
        )

        # ---- 悬赏协助：常驻，不受开关控制 ----
        self._xuanshang_worker: Optional[XuanshangWorker] = None
        try:
            self._xuanshang_worker = XuanshangWorker(
                device, log_adapter,
                on_slot=self._on_popup_slot,
                target_res=base_res,
            )
            self._xuanshang_worker.start()
        except Exception as e:
            self._reporter.log(f'XuanshangWorker 启动失败: {e}', 'error')
            self._xuanshang_worker = None

        # ---- 跳过结算：受 switch_1 控制 ----
        self._jixu_worker: Optional[JixuWorker] = None
        try:
            self._jixu_worker = JixuWorker(
                device, event_bus, log_adapter,
                target_res=base_res,
            )
            self._jixu_worker.start()
        except Exception as e:
            self._reporter.log(f'JixuWorker 启动失败: {e}', 'error')
            self._jixu_worker = None

        # ---- 用 config 里的 switch_1.default 初始化 JixuWorker 开关 ----
        default_switch_1 = self._read_switch_default(0, True)
        if self._jixu_worker is not None:
            try:
                self._jixu_worker.set_enabled(default_switch_1)
            except Exception:
                pass

        self._auto_scheduler: Optional[AutoScheduler] = None
        try:
            self._auto_scheduler = AutoScheduler(event_bus, device)
        except Exception as e:
            self._reporter.log(f'AutoScheduler 启动失败: {e}', 'error')
            self._auto_scheduler = None

        # 订阅 worker 事件
        self._unsub_worker_log = self._event_bus.subscribe(
            WorkerLogEvent, self._on_worker_log)
        self._unsub_worker_done = self._event_bus.subscribe(
            WorkerCompletedEvent, self._on_worker_completed)
        self._unsub_worker_fail = self._event_bus.subscribe(
            WorkerPermanentlyFailedEvent, self._on_worker_failed)
        self._unsub_worker_health = self._event_bus.subscribe(
            WorkerHealthEvent, self._on_worker_health)

        self._set_state(BossState.IDLE, '')

    # ==================================================================
    #  座位
    # ==================================================================

    def _read_switch_default(self, index: int, fallback: bool) -> bool:
        """读 slots.switches[index].default，读不到返回 fallback"""
        switches = (self._slots or {}).get('switches', []) or []
        if index < 0 or index >= len(switches):
            return fallback
        return bool(switches[index].get('default', fallback))

    def _collect_switch_defaults(self) -> Dict[str, bool]:
        result = {}
        switches = (self._slots or {}).get('switches', []) or []
        for i, sw in enumerate(switches):
            slot_name = f'switch_{i + 1}'
            result[slot_name] = bool(sw.get('default', False))
        return result

    def report_task_list(self):
        count = len(self._tasks)
        self._reporter.log(f'✅ 已获取方案列表（{count} 个）', 'info')

    def report_slot_state(self):
        """上报座位初始状态（绑定完成后由 host 调用）。"""
        defaults = self._collect_switch_defaults()

        # switch_1（全局处理 = 跳过结算）以 JixuWorker 实际值为准
        if self._jixu_worker is not None:
            defaults['switch_1'] = bool(self._jixu_worker.enabled)

        for slot_name, value in defaults.items():
            self._switch_values[slot_name] = value
            try:
                self._reporter.slot(slot_name, value)
            except Exception:
                pass

        # 悬赏协助：补发初始状态，让悬浮窗第 0 行有值
        if self._xuanshang_worker is not None:
            try:
                self._xuanshang_worker.report_status()
            except Exception:
                pass

    def _on_popup_slot(self, slot_name: str, value):
        try:
            self._reporter.slot(slot_name, value)
        except Exception:
            pass

    def on_slot_command(self, slot_name: str, value) -> None:
        """
        处理 UI 座位操作：
            switch_1  → JixuWorker.set_enabled（跳过结算开关）
            switch_2  → 空壳（掉线处理）
        """
        if slot_name == 'switch_1':
            enabled = bool(value)
            if self._jixu_worker is not None:
                try:
                    self._jixu_worker.set_enabled(enabled)
                    actual = bool(self._jixu_worker.enabled)
                except Exception:
                    actual = enabled
            else:
                actual = enabled
            self._switch_values['switch_1'] = actual
            try:
                self._reporter.slot('switch_1', actual)
            except Exception:
                pass

        elif slot_name == 'switch_2':
            enabled = bool(value)
            self._switch_values['switch_2'] = enabled
            try:
                self._reporter.slot('switch_2', enabled)
            except Exception:
                pass

    # ==================================================================
    #  IBoss 接口
    # ==================================================================

    @property
    def name(self) -> str:
        return '阴阳师'

    def start_task(self, task_id: str, max_count: str = 'inf') -> bool:
        with self._lock:
            if task_id not in self._tasks:
                self._reporter.log(f'未知任务 {task_id}', 'error')
                return False

            if self._running_task_id:
                spec = self._tasks.get(self._running_task_id)
                name = spec.name if spec else self._running_task_id
                self._reporter.log(
                    f'当前任务（{name}）在运行，请先停止', 'warning')
                return False

            if self._pending_start is not None:
                self._reporter.log(
                    '上一次启动还在初始化中，请稍候', 'warning')
                return False

            spec = self._tasks[task_id]

        if self._worker_launcher is None:
            self._reporter.log('未注入 WorkerLauncher', 'error')
            return False

        ctx = self._tools.device_context if self._tools else {}
        module_name = spec.module_file.replace('.py', '')
        full_module = f'plugins.{self._game_folder}.{spec.folder}.{module_name}'

        templates_dir = (
            ctx.get('templates_dir', '')
            or self._templates_dir
            or ''
        )

        # 引擎发黄：初始化中
        self._reporter.log(f'⚠ 初始化中 {spec.name}', 'info')

        try:
            ok = self._worker_launcher.launch(
                worker_id=self._device,
                device=self._device,
                task_module=full_module,
                task_class=spec.class_name,
                hwnd_parent=ctx.get('parent_hwnd', 0),
                hwnd_click=ctx.get('click_hwnd', 0),
                hwnd_shot=ctx.get('shot_hwnd', 0),
                shm_name=ctx.get('shm_name', ''),
                window_w=ctx.get('window_w', 0),
                window_h=ctx.get('window_h', 0),
                templates_dir=templates_dir,
                max_count=max_count,
            )
        except Exception as e:
            self._reporter.log(
                f'拉起 Worker 异常: {e}', 'error',
                ui_message=f'❌ 启动失败 {spec.name}: 拉起进程异常',
            )
            return False

        if not ok:
            self._reporter.log(
                '拉起 Worker 失败', 'error',
                ui_message=f'❌ 启动失败 {spec.name}: 拉起进程失败',
            )
            return False

        # launch 成功 → 挂 pending + 起超时定时器
        timer = threading.Timer(self._START_TIMEOUT, self._on_start_timeout)
        timer.daemon = True
        with self._lock:
            self._pending_start = {
                'task_id': task_id,
                'spec': spec,
                'timer': timer,
            }
        timer.start()

        return True

    def _on_worker_health(self, evt):
        """worker 第一次心跳 → 启动完成，挂绿牌子"""
        if self._event_device(evt) != self._device:
            return
        with self._lock:
            pending = self._pending_start
            if pending is None:
                return
            self._pending_start = None
            self._running_task_id = pending['task_id']
            timer = pending['timer']
            spec = pending['spec']
        
        timer.cancel()
        self._set_state(BossState.RUNNING, '')
        self._reporter.log(f'🚩 启动任务 {spec.name}', 'info')

    def _on_start_timeout(self):
        """心跳超时 → 启动失败"""
        with self._lock:
            pending = self._pending_start
            if pending is None:
                return
            self._pending_start = None
        spec = pending['spec']
        # 主动 terminate，避免孤儿进程
        try:
            self._worker_launcher.terminate(
                self._device, reason='no health')
        except Exception:
            pass
        self._set_state(BossState.ERROR, '启动超时')
        self._reporter.log(
            f'❌ 启动失败 {spec.name}: 无心跳', 'error')

    def _clear_pending_start(self, reason: str) -> bool:
        """
        如果正在等心跳，取消定时器 + 走启动失败路径。
        返回 True 表示处理了 pending；False 表示没有 pending。
        """
        with self._lock:
            pending = self._pending_start
            if pending is None:
                return False
            self._pending_start = None
        pending['timer'].cancel()
        spec = pending['spec']
        self._set_state(BossState.ERROR, f'启动失败: {reason}')
        self._reporter.log(
            f'❌ 启动失败 {spec.name}: {reason}', 'error',
            ui_message=f'❌ 启动失败 {spec.name}',
        )
        return True

    def _on_worker_completed(self, evt):
        """worker 进程正常退出（returncode=0）。
    
        success=True  → 任务自然完成 / 用户主动停后的收尾
                        → Boss 流程正常结束
        success=False → worker 内部 task.run() 抛异常，进程正常退出
                        → Boss 流程被异常打断
        """
        if self._event_device(evt) != self._device:
            return
        # 心跳没到就退出 → 启动失败
        if self._clear_pending_start('进程提前退出'):
            return
        with self._lock:
            if not self._running_task_id:
                return
            spec = self._tasks.get(self._running_task_id)
            name = spec.name if spec else self._running_task_id
            self._running_task_id = ''
            if evt.success:
                self._set_state(BossState.IDLE, f'{name} 已结束')
                self._reporter.log(f'⏹️ 任务结束 {name}', 'info')
            else:
                reason = getattr(evt, 'message', '') or '未知原因'
                self._set_state(BossState.ERROR, f'{name} 被中断: {reason}')
                self._reporter.log(f'❌ 任务被中断 {name}: {reason}', 'error')
    
    def _on_worker_failed(self, evt):
        """worker 进程异常退出（returncode≠0）。

        → Boss 流程被异常打断
        """
        if self._event_device(evt) != self._device:
            return
        reason = getattr(evt, 'reason', '') or '进程异常退出'
        # 心跳没到就退出 → 启动失败
        if self._clear_pending_start(reason):
            return
        with self._lock:
            if not self._running_task_id:
                return
            spec = self._tasks.get(self._running_task_id)
            name = spec.name if spec else self._running_task_id
            self._running_task_id = ''
            self._set_state(BossState.ERROR, f'{name} 被中断: {reason}')
            self._reporter.log(
                f'❌ 任务被中断 {name}: {reason}',
                'error',
                ui_message=f'❌ 任务被中断 {name}: 进程退出',
            )

    def stop_task(self, task_id: str = '') -> None:
        # 1. 若在等心跳 → 取消 pending + terminate
        with self._lock:
            pending = self._pending_start
            if pending is not None:
                self._pending_start = None
        if pending is not None:
            pending['timer'].cancel()
            try:
                self._worker_launcher.terminate(
                    self._device, reason='stop during start')
            except Exception:
                pass
            self._set_state(BossState.IDLE, '')
            self._reporter.log(
                f'⏹️ 停止任务 {pending["spec"].name}', 'info')
            return

        # 2. 正常运行中停止
        with self._lock:
            if not self._running_task_id:
                return
            if task_id and task_id != self._running_task_id:
                return

            spec = self._tasks.get(self._running_task_id)
            name = spec.name if spec else self._running_task_id
            self._set_state(BossState.STOPPING, f'{name} 停止中')
            self._reporter.log(f'⏹️ 停止任务 {name}', 'info')

            # 关键：先清 _running_task_id，再出锁 terminate。
            # terminate 期间 worker 可能发来 WorkerCompletedEvent，
            # _on_worker_completed 看到 _running_task_id 为空会直接 return，
            # 不会把这次停当成"任务自然结束"再处理一遍。
            self._running_task_id = ''

        # 出锁 terminate（可能阻塞 2s，不占锁）
        if self._worker_launcher is not None:
            try:
                self._worker_launcher.terminate(
                    self._device, reason='boss stop')
            except Exception:
                pass

        # _set_state(IDLE) 放 terminate 之后：
        # 避免 UI 先收到 IDLE，又在 terminate 期间被完成事件覆盖。
        self._set_state(BossState.IDLE, '')

    def pause(self) -> None:
        with self._lock:
            if self._state != BossState.RUNNING:
                return
            self._set_state(BossState.HANGING, '已暂停')

    def resume(self) -> None:
        with self._lock:
            if self._state != BossState.HANGING:
                return
            self._set_state(BossState.RUNNING, '已恢复')

    def shutdown(self) -> None:
        with self._lock:
            self._stop_event.set()
            pending = self._pending_start
            self._pending_start = None
        if pending is not None:
            pending['timer'].cancel()

        if self._xuanshang_worker is not None:
            try:
                self._xuanshang_worker.stop()
            except Exception:
                pass
            self._xuanshang_worker = None

        if self._jixu_worker is not None:
            try:
                self._jixu_worker.stop()
            except Exception:
                pass
            self._jixu_worker = None

        if self._auto_scheduler is not None:
            try:
                self._auto_scheduler.shutdown()
            except Exception:
                pass
            self._auto_scheduler = None

        # 停 worker 子进程。
        # WorkerLauncher.terminate 是幂等的：
        # DeviceManager.unbind 之后会再调一次，第二次因记录已弹出而直接返回。
        if self._worker_launcher is not None:
            try:
                self._worker_launcher.terminate(
                    self._device, reason='boss shutdown')
            except Exception:
                pass

        if getattr(self, '_unsub_worker_log', None):
            try:
                self._unsub_worker_log()
            except Exception:
                pass
            self._unsub_worker_log = None

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

        if getattr(self, '_unsub_worker_health', None):
            try:
                self._unsub_worker_health()
            except Exception:
                pass
            self._unsub_worker_health = None

        self._set_state(BossState.IDLE, '')

    # ==================================================================
    #  内部
    # ==================================================================

    def _event_device(self, evt) -> str:
        return (
            getattr(evt, 'worker_id', '')
            or getattr(evt, 'device', '')
            or getattr(evt, 'sender', '')
        )

    def _on_worker_log(self, evt):
        if self._event_device(evt) != self._device:
            return
        quiet = bool(getattr(evt, 'quiet', False))
        self._reporter.log(evt.message, evt.level or 'info', quiet=quiet)

    def _set_state(self, state: BossState, detail: str = '') -> None:
        self._state = state
        self._state_detail = detail
        try:
            self._reporter.state(state, detail)
        except Exception:
            pass