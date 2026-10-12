# plugins/yys/_shared/navigation_engine.py
"""
NavigationEngine —— 智能导航（业务层）

识别庭院、探索、町中、突破，未知界面自动恢复。

所有坐标统一为当前基准坐标（由 TemplateManager.base_res 决定）。
- 模板匹配走 TemplateManager（内部已按当前截图分辨率缩放，返回基准坐标）
- 动态按钮搜索区域从 source_<base>.json 的 coords/navigation 读取
- 颜色/亮度/白黑比等算法参数保留在代码中
- device_utils.tap() 接收基准坐标，内部映射到实际窗口

依赖：
    capability.device_utils
    capability.image_center
"""
import time
import random
import cv2
import numpy as np

from capability.device_utils import (
    take_screenshot_np, tap, normal_click_in_region,
    get_active_base_resolution,
)
from capability.image_center import TemplateManager


class Navigator:
    """智能导航：识别庭院、探索、町中、突破，未知界面自动恢复
    改动要点（v2）：
    - __init__ 加 target_res 参数。
      外部传入优先；不传时按 self.device 从 device_utils 取；
      都拿不到时退回 TemplateManager 默认（选最高基准）。
      与 device_utils.tap 用同一基准，避免多基准时坐标错位。
    """

    FIXED_WAIT = {
        ('tingyuan', 'tansuo'): 0.4,
        ('tingyuan', 'dingzhong'): 0.5,
        ('tansuo', 'tingyuan'): 0.5,
        ('dingzhong', 'tingyuan'): 0.5,
        ('unknown', 'tingyuan'): 0.5,
        ('tupo', 'tansuo'): 0.5,
    }

    def __init__(self, device, log_callback=None, stop_check_callback=None,
                 target_res=None):
        self.device = device
        self.log_callback = log_callback if log_callback else self._default_log
        self.stop_check_callback = stop_check_callback if stop_check_callback else (lambda: False)

        # ---------- 模板系统（多基准） ----------
        # 基准优先用外部传入；
        # 否则按 device 自己取（与 device_utils.tap 对齐）；
        # 都拿不到时退回 TemplateManager 默认（选最高基准）。
        if target_res is None:
            try:
                target_res = get_active_base_resolution(self.device)
            except Exception:
                target_res = None
        self.tm = TemplateManager(target_res=target_res)

        # ---------- 确认弹窗模板集合 ----------
        self.CONFIRM_NAMES = [
            ('common', 'tansuotuichuqueren'),
            ('common', 'queding'),
            ('collaboration', 'zhandoutuichuqueren'),
        ]

        # ---------- 回城模板集合 ----------
        self.HUICHENG_NAMES = [
            ('common', 'huicheng'),
            ('common', 'huicheng0'),
            ('common', 'huicheng1'),
            ('common', 'huicheng2'),
        ]

        # ---------- 突破退出模板 ----------
        self.TUPO_EXIT_NAMES = [
            ('collaboration', 'jiejietuichu'),
        ]

        # ---------- 动态按钮参数（坐标从 coords 读；算法参数保留） ----------
        self.btn_tansuo = self._build_btn_config(
            'tingyuan_tansuo',
            w=150, h=190,
            target_bgr=(163, 214, 231), bgr_tol=20,
            brightness_min=195,
            white_ratio_min=0.28, black_ratio_max=0.15,
        )
        self.btn_dingzhong = self._build_btn_config(
            'tingyuan_dingzhong',
            w=170, h=130,
            target_bgr=(141, 150, 156), bgr_tol=12,
            brightness_min=145, brightness_max=160,
            white_ratio_max=0.05, black_ratio_max=0.12,
        )
        self.btn_tingyuan_from_dingzhong = self._build_btn_config(
            'dingzhong_tingyuan',
            w=170, h=160,
            target_bgr=(140, 148, 155), bgr_tol=12,
            brightness_min=145, brightness_max=156,
            white_ratio_max=0.05, black_ratio_max=0.12,
        )

        # 屏幕缓存
        self._last_screen = None
        self._last_screen_time = 0.0

    # ==================================================================
    #  coords 读取
    # ==================================================================

    def _get_btn_region(self, name):
        """从 coords/navigation/<name> 读基准区域。读不到返回 None。"""
        try:
            entry = self.tm.get_coord('navigation', name)
        except Exception:
            entry = None
        if entry and entry.get('click') and len(entry['click']) == 4:
            return tuple(int(v) for v in entry['click'])
        return None

    def _build_btn_config(self, coord_name, **kwargs):
        r = self._get_btn_region(coord_name)
        if r is None:
            self._log(f'⚠ 导航按钮 {coord_name} 坐标未配置，本次跳过')
            return None
        cfg = {'x1': r[0], 'y1': r[1], 'x2': r[2], 'y2': r[3]}
        cfg.update(kwargs)
        return cfg

    # ==================================================================
    #  基础工具
    # ==================================================================

    def _default_log(self, msg):
        print(msg)

    def _log(self, msg):
        if self.log_callback:
            self.log_callback(msg)

    def _get_cached_screen(self, max_age=0.5):
        now = time.time()
        if self._last_screen is not None and (now - self._last_screen_time) < max_age:
            return self._last_screen
        screen = take_screenshot_np(self.device)
        if screen is not None:
            self._last_screen = screen
            self._last_screen_time = now
        return screen

    def invalidate_screen(self):
        """主动让截图缓存失效。

        在点击、滑动、长等待之后调用，避免下一轮读到旧画面。
        """
        self._last_screen = None
        self._last_screen_time = 0.0

    def _scale_region_4k_to_screen(self, region_4k, screen):
        x1, y1, x2, y2 = region_4k
        h, w = screen.shape[:2]
        base_w, base_h = self.tm.base_res
        sx = w / base_w
        sy = h / base_h
        return (int(x1 * sx), int(y1 * sy), int(x2 * sx), int(y2 * sy))

    def _click_region_4k(self, screen, region_4k):
        region = self._scale_region_4k_to_screen(region_4k, screen)
        cx, cy = normal_click_in_region(region)
        h, w = screen.shape[:2]
        base_w, base_h = self.tm.base_res
        bx = int(cx * base_w / w)
        by = int(cy * base_h / h)
        tap(self.device, bx, by)

    def _click_screen_pos(self, screen, sx, sy):
        h, w = screen.shape[:2]
        base_w, base_h = self.tm.base_res
        bx = int(sx * base_w / w)
        by = int(sy * base_h / h)
        tap(self.device, bx, by)

    # ==================================================================
    #  界面识别
    # ==================================================================

    def _get_color_features(self, screen):
        b = screen[:, :, 0].astype(np.int16)
        g = screen[:, :, 1].astype(np.int16)
        r = screen[:, :, 2].astype(np.int16)
        total_pixels = r.size

        dark_mask = (r < 60) & (g < 60) & (b < 60)
        dark_ratio = float(np.sum(dark_mask)) / total_pixels

        blue_purple_mask = (b > r + 25) & (b > g + 15) & (b > 80)
        blue_purple_ratio = float(np.sum(blue_purple_mask)) / total_pixels
        return blue_purple_ratio, dark_ratio

    def is_interface_visible(self, screen, name):
        if screen is None:
            return False

        if name == 'tansuo':
            return self.tm.exists(screen, 'common', 'tansuo')

        if name == 'tupo':
            if self.tm.exists(screen, 'collaboration', 'gerentupo'):
                return True
            if self.tm.exists(screen, 'liaotupo', 'liaotupo'):
                return True
            return False

        if name == 'tingyuan':
            if not self.tm.exists(screen, 'common', 'tingyuan'):
                return False
            blue_ratio, dark_ratio = self._get_color_features(screen)
            return blue_ratio > 0.40 and dark_ratio < 0.20

        if name == 'dingzhong':
            if not self.tm.exists(screen, 'common', 'tingyuan'):
                return False
            blue_ratio, dark_ratio = self._get_color_features(screen)
            return blue_ratio < 0.10 and dark_ratio > 0.30

        return False

    def _detect_interface_once(self, screen):
        if screen is None:
            return 'unknown'
        if self.is_interface_visible(screen, 'tansuo'):
            return 'tansuo'
        if self.is_interface_visible(screen, 'tingyuan'):
            return 'tingyuan'
        if self.is_interface_visible(screen, 'dingzhong'):
            return 'dingzhong'
        if self.is_interface_visible(screen, 'tupo'):
            return 'tupo'
        return 'unknown'

    def detect_current_interface(self):
        screen = self._get_cached_screen(max_age=0.5)
        if screen is None:
            return 'unknown'
        return self._detect_interface_once(screen)

    # ==================================================================
    #  动态按钮检测
    # ==================================================================

    def _find_dynamic_button(self, screen, btn):
        """
        在基准区域的缩放版中查找按钮，返回屏幕分辨率下的中心坐标。
        btn 的 x1/x2/y1/y2/w/h 都是基准坐标。
        """
        h, w = screen.shape[:2]
        base_w, base_h = self.tm.base_res
        sx = w / base_w
        sy = h / base_h

        area_x1 = int(btn['x1'] * sx)
        area_x2 = int(btn['x2'] * sx)
        area_y1 = int(btn['y1'] * sy)
        area_y2 = int(btn['y2'] * sy)

        bw = int(btn['w'] * sx)
        bh = int(btn['h'] * sy)
        if bw < 3 or bh < 3:
            return None

        target_bgr = btn['target_bgr']
        bgr_tol = btn.get('bgr_tol', 20)
        brightness_min = btn.get('brightness_min', 0)
        brightness_max = btn.get('brightness_max', 999)
        white_ratio_min = btn.get('white_ratio_min', -1)
        white_ratio_max = btn.get('white_ratio_max', 2)
        black_ratio_max = btn.get('black_ratio_max', 2)

        step_x_coarse = max(3, bw // 4)
        step_y_coarse = max(3, bh // 4)

        best_center = None
        best_score = -1.0

        for y in range(area_y1, area_y2 - bh + 1, step_y_coarse):
            for x in range(area_x1, area_x2 - bw + 1, step_x_coarse):
                roi = screen[y:y + bh, x:x + bw]
                if roi.shape[0] < bh or roi.shape[1] < bw:
                    continue

                gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
                brightness = float(np.mean(gray))
                if brightness < brightness_min or brightness > brightness_max:
                    continue

                avg_bgr = np.mean(roi.reshape(-1, 3), axis=0)
                color_diff = np.abs(avg_bgr - np.array(target_bgr))
                if np.any(color_diff > bgr_tol):
                    continue

                if white_ratio_min >= 0 or white_ratio_max <= 1:
                    white_mask = (roi[:, :, 0] > 180) & (roi[:, :, 1] > 180) & (roi[:, :, 2] > 180)
                    white_ratio = float(np.sum(white_mask)) / white_mask.size
                    if white_ratio < white_ratio_min or white_ratio > white_ratio_max:
                        continue

                if black_ratio_max <= 1:
                    black_mask = (roi[:, :, 0] < 80) & (roi[:, :, 1] < 80) & (roi[:, :, 2] < 80)
                    black_ratio = float(np.sum(black_mask)) / black_mask.size
                    if black_ratio > black_ratio_max:
                        continue

                fine_x1 = max(area_x1, x - step_x_coarse)
                fine_x2 = min(area_x2 - bw + 1, x + step_x_coarse)
                fine_y1 = max(area_y1, y - step_y_coarse)
                fine_y2 = min(area_y2 - bh + 1, y + step_y_coarse)

                step_x_fine = max(1, bw // 10)
                step_y_fine = max(1, bh // 10)

                for fy in range(fine_y1, fine_y2 + 1, step_y_fine):
                    for fx in range(fine_x1, fine_x2 + 1, step_x_fine):
                        roi_fine = screen[fy:fy + bh, fx:fx + bw]
                        if roi_fine.shape[0] < bh or roi_fine.shape[1] < bw:
                            continue

                        gray_fine = cv2.cvtColor(roi_fine, cv2.COLOR_BGR2GRAY)
                        brightness_fine = float(np.mean(gray_fine))
                        if brightness_fine < brightness_min or brightness_fine > brightness_max:
                            continue

                        avg_bgr_fine = np.mean(roi_fine.reshape(-1, 3), axis=0)
                        color_diff_fine = np.abs(avg_bgr_fine - np.array(target_bgr))
                        if np.any(color_diff_fine > bgr_tol):
                            continue

                        center_x = fx + bw // 2
                        center_y = fy + bh // 2
                        score = 1.0 - abs(brightness_fine - ((brightness_min + brightness_max) / 2)) / max(
                            1, (brightness_max - brightness_min) / 2)
                        if score > best_score:
                            best_score = score
                            best_center = (center_x, center_y)

        return best_center

    def _handle_confirm_buttons(self):
        """遍历确认弹窗，找到就按 click_mode 点击"""
        screen = self._get_cached_screen(max_age=0.5)
        if screen is None:
            return False
        for plugin, name in self.CONFIRM_NAMES:
            if self.tm.match_and_click(
                screen, plugin, name,
                tap_func=lambda x, y: tap(self.device, x, y),
                normal_click_func=normal_click_in_region,
            ):
                self._log('👉 点击确认')
                self.invalidate_screen()
                return True
        return False

    # ==================================================================
    #  辅助动作（回城 / 突破退出）
    # ==================================================================

    def _try_huicheng_once(self):
        """尝试点一次回城（huicheng 系列），不等待"""
        screen = self._get_cached_screen(max_age=0.5)
        if screen is None:
            return False
        for plugin, name in self.HUICHENG_NAMES:
            if self.tm.match_and_click(
                screen, plugin, name,
                tap_func=lambda x, y: tap(self.device, x, y),
                normal_click_func=normal_click_in_region,
            ):
                self._log('👉 点击回城')
                self.invalidate_screen()
                return True
        return False

    def _try_tupo_exit_once(self):
        """尝试点一次突破退出（collaboration/jiejietuichu）"""
        screen = self._get_cached_screen(max_age=0.5)
        if screen is None:
            return False
        for plugin, name in self.TUPO_EXIT_NAMES:
            if self.tm.match_and_click(
                screen, plugin, name,
                tap_func=lambda x, y: tap(self.device, x, y),
                normal_click_func=normal_click_in_region,
            ):
                self._log('👉 点击突破退出')
                self.invalidate_screen()
                return True
        return False

    # ==================================================================
    #  未知界面恢复
    # ==================================================================

    def _recover_from_unknown(self):
        """
        未知界面恢复：
        每轮按顺序：检测目标界面 → 确认弹窗 → 回城模板
        """
        self._log('🔄 未知界面恢复中...')
        start_time = time.time()

        while not self.stop_check_callback():
            if time.time() - start_time > 30:
                self._log('❌ 未找到回城')
                return False

            screen = self._get_cached_screen(max_age=0.5)
            if screen is None:
                time.sleep(random.randint(25, 35) / 100)
                continue

            if self.is_interface_visible(screen, 'tingyuan'):
                self._log('🏁 已到达庭院')
                return True
            if self.is_interface_visible(screen, 'tansuo'):
                self._log('🏁 已到达探索')
                return True
            if self.is_interface_visible(screen, 'dingzhong'):
                self._log('🏁 已到达町中')
                return True

            handled_popup = False
            for plugin, name in self.CONFIRM_NAMES:
                if self.tm.match_and_click(
                    screen, plugin, name,
                    tap_func=lambda x, y: tap(self.device, x, y),
                    normal_click_func=normal_click_in_region,
                ):
                    self._log('👉 点击确认')
                    handled_popup = True
                    # sleep 只有 0.25~0.35s，短于 0.5s 缓存窗口；
                    # 不 invalidate 的话下一轮可能读到点之前的画面，重复点确认。
                    self.invalidate_screen()
                    break

            if handled_popup:
                time.sleep(random.randint(25, 35) / 100)
                continue

            if self._try_huicheng_once():
                if self._wait_after_huicheng():
                    return True
                else:
                    return False

            # 兜底：无目标界面、无确认弹窗、无回城按钮时，尝试点 coords/common/tuichu
            tuichu = self.tm.get_coord('common', 'tuichu')
            if tuichu and tuichu.get('click') and len(tuichu['click']) == 4:
                self._click_region_4k(screen, tuple(tuichu['click']))
                self._log('👉 点击退出')
                # 清掉截图缓存，避免下一轮还用旧图重复点
                self.invalidate_screen()
                time.sleep(random.randint(100, 150) / 100)
                continue

            time.sleep(random.randint(100, 150) / 100)

        return False

    def _wait_after_huicheng(self):
        start_time = time.time()
        while not self.stop_check_callback():
            if time.time() - start_time > 30:
                return False

            screen = self._get_cached_screen(max_age=0.5)
            if screen is None:
                time.sleep(random.randint(25, 35) / 100)
                continue

            if self.is_interface_visible(screen, 'tingyuan'):
                self._log('🏁 已到达庭院')
                return True

            handled = False
            for plugin, name in self.CONFIRM_NAMES:
                if self.tm.match_and_click(
                    screen, plugin, name,
                    tap_func=lambda x, y: tap(self.device, x, y),
                    normal_click_func=normal_click_in_region,
                ):
                    self._log('👉 点击确认')
                    handled = True
                    break

            if handled:
                time.sleep(2)
            else:
                time.sleep(random.randint(25, 35) / 100)
        return False

    # ==================================================================
    #  导航核心
    # ==================================================================

    def _goto(self, source, target, button_type=None, max_rounds=3):
        fixed_wait = self.FIXED_WAIT.get((source, target), 5)

        for round_num in range(max_rounds):
            if self.stop_check_callback():
                return False

            screen = self._get_cached_screen(max_age=0.5)
            if screen is None:
                time.sleep(random.randint(25, 35) / 100)
                continue

            if not self.is_interface_visible(screen, source):
                time.sleep(random.randint(25, 35) / 100)
                continue

            if self.stop_check_callback():
                return False

            if button_type is not None:
                found_center = self._find_dynamic_button(screen, button_type)
                if found_center:
                    self._click_screen_pos(screen, found_center[0], found_center[1])
                    self._log('👉 点击动态按钮')
                    # 点击后立刻失效缓存：下一轮必须看到新画面
                    self.invalidate_screen()
                else:
                    self._log('❌ 动态按钮未找到')

            wait_start = time.time()
            while time.time() - wait_start < fixed_wait:
                if self.stop_check_callback():
                    return False
                time.sleep(0.1)
            # 长等待后失效缓存：等待期间画面可能已变
            self.invalidate_screen()

            start = time.time()
            timeout = 2.0
            while time.time() - start < timeout:
                if self.stop_check_callback():
                    return False
                time.sleep(random.randint(25, 35) / 100)
                screen = self._get_cached_screen(max_age=0.5)
                if screen is None:
                    continue
                if target != 'unknown' and self.is_interface_visible(screen, target):
                    return True

        return False

    # ==================================================================
    #  对外导航方法
    # ==================================================================

    def _try_go_to_tansuo_once(self):
        current = self.detect_current_interface()
        if current == 'tansuo':
            return True
        elif current == 'tupo':
            # 突破 → 探索：走 collaboration/jiejietuichu 模板
            for _ in range(5):
                if self.stop_check_callback():
                    return False
                if self._try_tupo_exit_once():
                    time.sleep(0.5)
                    screen = self._get_cached_screen(max_age=0.5)
                    if screen is not None and self.is_interface_visible(screen, 'tansuo'):
                        return True
                time.sleep(0.5)
            return False
        elif current == 'tingyuan':
            return self._goto(
                'tingyuan', 'tansuo',
                button_type=self.btn_tansuo,
            )
        elif current == 'dingzhong':
            if self._goto(
                'dingzhong', 'tingyuan',
                button_type=self.btn_tingyuan_from_dingzhong,
            ):
                return self._goto(
                    'tingyuan', 'tansuo',
                    button_type=self.btn_tansuo,
                )
            return False
        elif current == 'unknown':
            if self._recover_from_unknown():
                return self._goto(
                    'tingyuan', 'tansuo',
                    button_type=self.btn_tansuo,
                )
            return False
        return False

    def go_to_tansuo(self):
        self._log('🚗 智能导航中...')
        for attempt in range(3):
            if self.stop_check_callback():
                return False
            if self._try_go_to_tansuo_once():
                self._log('🏁 已到达探索')
                return True
            if attempt < 2:
                self._handle_confirm_buttons()
                time.sleep(0.5)
        self._log('❌ 导航失败')
        return False

    def _try_go_to_tingyuan_once(self):
        current = self.detect_current_interface()
        if current == 'tingyuan':
            return True
        elif current == 'tansuo':
            # 探索 → 庭院：走 huicheng 4 个模板
            return self._go_to_tingyuan_by_huicheng()
        elif current == 'dingzhong':
            return self._goto(
                'dingzhong', 'tingyuan',
                button_type=self.btn_tingyuan_from_dingzhong,
            )
        elif current == 'tupo':
            # 突破 → 探索 → 庭院
            for _ in range(5):
                if self.stop_check_callback():
                    return False
                if self._try_tupo_exit_once():
                    time.sleep(0.5)
                    screen = self._get_cached_screen(max_age=0.5)
                    if screen is not None and self.is_interface_visible(screen, 'tansuo'):
                        break
                time.sleep(0.5)
            else:
                return False
            return self._go_to_tingyuan_by_huicheng()
        elif current == 'unknown':
            return self._recover_from_unknown()
        return False

    def _go_to_tingyuan_by_huicheng(self):
        """当前在探索（或可点回城的状态），循环点 huicheng 直到庭院"""
        for _ in range(5):
            if self.stop_check_callback():
                return False
            if self._try_huicheng_once():
                if self._wait_after_huicheng():
                    return True
                return False
            time.sleep(0.5)
        return False

    def go_to_tingyuan(self):
        self._log('🚗 智能导航中...')
        for attempt in range(3):
            if self.stop_check_callback():
                return False
            if self._try_go_to_tingyuan_once():
                self._log('🏁 已到达庭院')
                return True
            if attempt < 2:
                self._handle_confirm_buttons()
                time.sleep(0.5)
        self._log('❌ 导航失败')
        return False

    def _try_go_to_dingzhong_once(self):
        current = self.detect_current_interface()
        if current == 'dingzhong':
            return True
        elif current == 'tingyuan':
            return self._goto(
                'tingyuan', 'dingzhong',
                button_type=self.btn_dingzhong,
            )
        elif current == 'tansuo':
            # 探索 → 庭院 → 町中
            if not self._go_to_tingyuan_by_huicheng():
                return False
            return self._goto(
                'tingyuan', 'dingzhong',
                button_type=self.btn_dingzhong,
            )
        elif current == 'tupo':
            # 突破 → 探索 → 庭院 → 町中
            for _ in range(5):
                if self.stop_check_callback():
                    return False
                if self._try_tupo_exit_once():
                    time.sleep(0.5)
                    screen = self._get_cached_screen(max_age=0.5)
                    if screen is not None and self.is_interface_visible(screen, 'tansuo'):
                        break
                time.sleep(0.5)
            else:
                return False
            if not self._go_to_tingyuan_by_huicheng():
                return False
            return self._goto(
                'tingyuan', 'dingzhong',
                button_type=self.btn_dingzhong,
            )
        elif current == 'unknown':
            if self._recover_from_unknown():
                return self._goto(
                    'tingyuan', 'dingzhong',
                    button_type=self.btn_dingzhong,
                )
            return False
        return False

    def go_to_dingzhong(self):
        self._log('🚗 智能导航中...')
        for attempt in range(3):
            if self.stop_check_callback():
                return False
            if self._try_go_to_dingzhong_once():
                self._log('🏁 已到达町中')
                return True
            if attempt < 2:
                self._handle_confirm_buttons()
                time.sleep(0.5)
        self._log('❌ 导航失败')
        return False