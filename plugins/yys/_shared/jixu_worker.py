# plugins/yys/_shared/jixu_worker.py
"""
JixuWorker —— 跳过结算处理器（业务层）

职责：
    受"全局处理"开关（switch_1）控制。
    订阅 AutoScheduler 的 Pause/Resume 事件（只影响自己）。
    检测 jixu 系列模板 → 点击"继续"。

中立：
    本模块不知道 UI 长什么样。

改动要点（v2）：
    __init__ 新增 target_res 参数：
    业务 boss 会把 device_utils 当前设备的 base_res 传进来，
    使 TemplateManager 与 device_utils.tap 使用同一个基准。
    否则 TemplateManager() 无参构造按"最高基准"选，
    多基准时会与 device_utils 按窗口高度选的基准不一致，
    导致 tap 坐标缩放系数错位。

依赖：
    capability.device_utils / capability.image_center
    infra.log_manager
    contracts.core / contracts.host
    plugins.yys._shared.resources
"""
import random
import threading
import time

from capability.device_utils import (
    take_screenshot_np, tap, normal_click_in_region,
)
from capability.image_center import TemplateManager

from contracts.core import IEventBus
from contracts.host import PauseResourceEvent, ResumeResourceEvent
from plugins.yys._shared.resources import ResourceNames


class JixuWorker(threading.Thread):
    """跳过结算处理器（每设备一个实例）"""

    RESET_TIMEOUT = 4.0
    MAX_CLICKS = 3
    JIXU_NAMES = ['jixu0', 'jixu2', 'zhandoushibai']

    def __init__(self, device: str, event_bus: IEventBus, log_manager,
                 target_res=None):
        super().__init__(name=f'JixuWorker-{device}', daemon=True)
        self.device = device
        self._bus = event_bus
        self._log = log_manager

        self._stop_event = threading.Event()
        self._state_lock = threading.Lock()
        # target_res 与 device_utils 用同一个基准（见 docstring）
        self.tm = TemplateManager(target_res=target_res)

        self.in_settlement = False
        self.last_found_time = 0.0
        self.click_cooldown_until = 0.0
        self._continue_paused = False

        # 总开关（默认开；boss 会用 config 的 switch_1.default 覆盖）
        self._enabled = True

        # 订阅 AutoScheduler 的 Pause/Resume
        self._unsub_pause = self._bus.subscribe(
            PauseResourceEvent, self._on_pause_resource)
        self._unsub_resume = self._bus.subscribe(
            ResumeResourceEvent, self._on_resume_resource)

    # ==================================================================
    #  开关
    # ==================================================================

    def set_enabled(self, enabled: bool) -> None:
        enabled = bool(enabled)
        with self._state_lock:
            if self._enabled == enabled:
                return
            self._enabled = enabled
        if enabled:
            self._log.info(self.device, '▶️ 跳过结算已开启')
        else:
            self._log.info(self.device, '⏹️ 跳过结算已关闭')

    @property
    def enabled(self) -> bool:
        with self._state_lock:
            return self._enabled

    # ==================================================================
    #  Pause / Resume
    # ==================================================================

    def _on_pause_resource(self, evt: PauseResourceEvent) -> None:
        if evt.device != self.device:
            return
        if evt.resource_name != ResourceNames.SKIP_SETTLEMENT:
            return
        self.pause()

    def _on_resume_resource(self, evt: ResumeResourceEvent) -> None:
        if evt.device != self.device:
            return
        if evt.resource_name != ResourceNames.SKIP_SETTLEMENT:
            return
        self.resume()

    def pause(self):
        with self._state_lock:
            if self._continue_paused:
                return
            self._continue_paused = True
        self._log.info(self.device, '⏸️ 跳过结算已暂停')

    def resume(self):
        with self._state_lock:
            if not self._continue_paused:
                return
            self._continue_paused = False
        self._log.info(self.device, '▶️ 跳过结算已恢复')

    def _is_paused(self) -> bool:
        with self._state_lock:
            return self._continue_paused

    # ==================================================================
    #  主循环
    # ==================================================================

    def run(self):
        while not self._stop_event.is_set():
            time.sleep(random.randint(25, 35) / 100)
            if self._stop_event.is_set():
                break

            if not self.enabled:
                continue

            if self._is_paused():
                continue

            if time.time() < self.click_cooldown_until:
                continue

            screen = take_screenshot_np(self.device)
            if screen is None:
                continue

            try:
                self._handle_click_continue(screen)
            except Exception:
                pass

    def _try_jixu(self, screen):
        for name in self.JIXU_NAMES:
            if self.tm.get_entry('common', name) is None:
                continue
            ok = self.tm.match_and_click(
                screen, 'common', name,
                tap_func=lambda x, y: tap(self.device, x, y),
                normal_click_func=normal_click_in_region,
            )
            if ok:
                return True
        return False

    def _jixu_visible(self, screen):
        for name in self.JIXU_NAMES:
            if self.tm.get_entry('common', name) is None:
                continue
            if self.tm.exists(screen, 'common', name):
                return True
        return False

    def _handle_click_continue(self, screen):
        if self._is_paused():
            return
        now = time.time()
        if now < self.click_cooldown_until:
            return

        if not self._jixu_visible(screen):
            if self.in_settlement and now - self.last_found_time >= self.RESET_TIMEOUT:
                self.in_settlement = False
                self.click_cooldown_until = 0.0
            return

        self.last_found_time = now
        self.in_settlement = True

        for attempt in range(self.MAX_CLICKS):
            screen_check = take_screenshot_np(self.device)
            if screen_check is None:
                break
            if not self._try_jixu(screen_check):
                break
            self._log.info(self.device, '📣 点击屏幕继续')
            if attempt == 0:
                time.sleep(random.randint(400, 600) / 1000)
            elif attempt == 1:
                time.sleep(random.randint(1200, 1500) / 1000)

        self.click_cooldown_until = time.time() + 2.0

    # ==================================================================
    #  关闭
    # ==================================================================

    def stop(self):
        self._stop_event.set()
        with self._state_lock:
            self._continue_paused = False
        try:
            if self._unsub_pause:
                self._unsub_pause()
        except Exception:
            pass
        try:
            if self._unsub_resume:
                self._unsub_resume()
        except Exception:
            pass
        self._unsub_pause = None
        self._unsub_resume = None
        if self.is_alive():
            self.join(timeout=3.0)