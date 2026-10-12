# plugins/yys/_shared/xuanshang_worker.py
"""
XuanshangWorker —— 悬赏协助处理器（业务层）

职责：
    常驻运行，不受任何开关控制。
    检测悬赏协助弹窗 → 判断类型 → 点击接受/拒绝 → 发 alert 信号。

中立：
    本模块不知道 UI 长什么样，只通过 on_slot 回调发信号。

改动要点（v4）：
    - __init__ 新增 target_res 参数：与 device_utils.tap 用同一基准，
      避免多基准时点击坐标缩放错位。
    - 截图失败 / 恢复：节流打日志（首次失败 warning，恢复 info）
    - 主循环异常：落 traceback 到 error 日志
    - _emit_slot 异常：落 warning
    - report_status 从写死的 0 改成读实际计数器；
    - _handle 每次接/拒后推一次 status，悬浮窗实时更新。
    - v5: 拒绝（无益）时不再发送 alert 信号，只保留计数与日志。

依赖：
    capability.device_utils / capability.image_center
    infra.log_manager
"""
import random
import threading
import time
import traceback

from capability.device_utils import (
    take_screenshot_np, tap, normal_click_in_region,
)
from capability.image_center import TemplateManager


class XuanshangWorker(threading.Thread):
    """悬赏协助处理器（每设备一个实例）"""

    POPUP_ALERT_COOLDOWN = 0.5

    XUANSHANG_POPUP  = ('common', 'xuanshangxiezhu')
    XUANSHANG_GOUYU  = ('common', 'gouyuxiezhu')
    XUANSHANG_TLXZ   = ('common', 'tilixiezhu')
    XUANSHANG_ACCEPT = ('common', 'xuanshangjieshou')
    XUANSHANG_REJECT = ('common', 'xuanshangjujue')

    def __init__(self, device: str, log_manager, on_slot=None,
                 target_res=None):
        super().__init__(name=f'XuanshangWorker-{device}', daemon=True)
        self.device = device
        self._log = log_manager
        self._on_slot = on_slot

        self._stop_event = threading.Event()
        self._last_alert_at = 0.0
        # target_res 与 device_utils 用同一个基准
        self.tm = TemplateManager(target_res=target_res)

        # 计数：接受 / 拒绝 各累计多少次。
        # run() 线程累加，report_status() 可能被 boss 主线程调用 → 加锁。
        self._counter_lock = threading.Lock()
        self._accepted_count = 0
        self._rejected_count = 0

    # ==================================================================
    #  主循环
    # ==================================================================

    def run(self):
        screenshot_failed_logged = False

        while not self._stop_event.is_set():
            time.sleep(random.randint(25, 35) / 100)
            if self._stop_event.is_set():
                break

            screen = take_screenshot_np(self.device)
            if screen is None:
                if not screenshot_failed_logged:
                    self._log.warning(
                        self.device, '📢 悬赏协助:截图失败')
                    screenshot_failed_logged = True
                continue
            if screenshot_failed_logged:
                self._log.info(
                    self.device, '📢 悬赏协助:截图已恢复')
                screenshot_failed_logged = False

            try:
                self._handle(screen)
            except Exception as e:
                self._log.error(
                    self.device,
                    f'❌ 悬赏协助处理异常: {e}\n'
                    f'{traceback.format_exc()}')

    def _handle(self, screen):
        if not self.tm.exists(screen, *self.XUANSHANG_POPUP):
            return False

        if self.tm.exists(screen, *self.XUANSHANG_GOUYU):
            label, target, accepted = '勾玉', self.XUANSHANG_ACCEPT, True
        elif self.tm.exists(screen, *self.XUANSHANG_TLXZ):
            label, target, accepted = '体力', self.XUANSHANG_ACCEPT, True
        else:
            label, target, accepted = '无益', self.XUANSHANG_REJECT, False

        clicked = self.tm.match_and_click(
            screen, *target,
            tap_func=lambda x, y: tap(self.device, x, y),
            normal_click_func=normal_click_in_region,
        )
        if not clicked:
            return False

        self._log.info(
            self.device,
            f'📢 悬赏{label}：{"接受" if accepted else "拒绝"}')

        # 计数 +1，并推送最新统计（悬浮窗第 0 行实时更新）
        if accepted:
            with self._counter_lock:
                self._accepted_count += 1
        else:
            with self._counter_lock:
                self._rejected_count += 1
        self.report_status()

        # 仅“接受”类悬赏才发 alert 信号；无益（拒绝）静默处理
        if accepted:
            now = time.time()
            if now - self._last_alert_at >= self.POPUP_ALERT_COOLDOWN:
                self._last_alert_at = now
                self._emit_slot('alert', 'blink')

        time.sleep(random.randint(20, 30) / 100)
        return True

    # ==================================================================
    #  外部调用
    # ==================================================================

    def report_status(self):
        with self._counter_lock:
            accepted = self._accepted_count
            rejected = self._rejected_count
        self._emit_slot(
            'status',
            f'📢 悬赏协助:接受:{accepted} 拒绝:{rejected}')

    def _emit_slot(self, slot_name: str, value):
        if self._on_slot is None:
            return
        try:
            self._on_slot(slot_name, value)
        except Exception as e:
            self._log.warning(
                self.device,
                f'❌ 发送座位信号失败 slot={slot_name}: {e}')

    # ==================================================================
    #  关闭
    # ==================================================================

    def stop(self):
        self._stop_event.set()
        if self.is_alive():
            self.join(timeout=3.0)