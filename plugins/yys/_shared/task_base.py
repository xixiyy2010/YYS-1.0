# plugins/yys/_shared/task_base.py
"""
任务基类（业务层）

所有模板来自 templates/source_<base>.json。
所有坐标都是当前基准的坐标（分辨率由 meta.resolution 决定）。

状态上报约定：
    enter_state(state_name) / exit_state(state_name)
    state_name 由 plugins.yys._shared.resources 的 StateNames 定义。

依赖：
    capability.device_utils
    capability.image_center
"""
import random
import time

import numpy as np

from capability.device_utils import (
    take_screenshot_np,
    tap as device_tap,
    swipe as device_swipe,
    normal_click_in_region,
    get_active_base_resolution,
)
from capability.image_center import (
    TemplateManager,
    expand_grid,
    grid_sample_point,
)

# image_center 的随机点击辅助：优先用公开名，回退到私有名（兼容旧版本）。
# 未来 image_center 暴露 gauss_in_rect / clamp_rect 后，本处无需改动。
try:
    from capability.image_center import gauss_in_rect, clamp_rect
except ImportError:
    from capability.image_center import (
        _gauss_in_rect as gauss_in_rect,
        _clamp_rect as clamp_rect,
    )


class BaseTask:
    """所有任务类的基类
    改动要点（v2）：
    - __init__ 按 self.device 从 device_utils 取 base_res，
      传给 TemplateManager，保证与 device_utils.tap 用同一基准。
    - 新增 wait_interface_switch：轮询等界面从 A 切到 B（双向验证）。
    - 新增 click_and_wait_switch：点按钮 + 轮询等切换（带点击重试）。
      模式：点一次 → 轮询窗口 → 看到目标成功 / 还在原地再点 /
      过渡中不点继续轮询。总超时 + 点击上限兜底。
    """

    def __init__(self, tab, log_callback=None, stats_callback=None, error_callback=None):
        self.tab = tab
        self.device = tab.device_serial
        self.running = False

        self.log = log_callback if log_callback else (lambda msg, quiet=False: None)
        self.stats = stats_callback if stats_callback else (lambda data: None)
        self.error = error_callback if error_callback else (lambda msg: None)

        # 与 device_utils.tap 用同一基准，避免多基准时坐标错位。
        # worker 子进程里 device_utils 已在 _setup_device 里 sync 过基准，
        # 直接按 self.device 读即可。
        try:
            _target_res = get_active_base_resolution(self.device)
        except Exception:
            _target_res = None
        self.tm = TemplateManager(target_res=_target_res)

    # ==================================================================
    #  模板匹配（基准坐标）
    # ==================================================================

    def match(self, plugin, name, screen=None, return_center=True):
        """匹配模板。return_center=True 返回基准坐标 (bx, by) 或 None"""
        if screen is None:
            screen = self.take_screenshot()
            if screen is None:
                return None if return_center else 0.0
        return self.tm.match(screen, plugin, name, return_center=return_center)

    def exists(self, plugin, name, screen=None):
        """判定模板是否匹配（阈值）"""
        if screen is None:
            screen = self.take_screenshot()
            if screen is None:
                return False
        return self.tm.exists(screen, plugin, name)

    def score(self, plugin, name, screen=None):
        """返回最高匹配分"""
        if screen is None:
            screen = self.take_screenshot()
            if screen is None:
                return 0.0
        return self.tm.score(screen, plugin, name)

    def compute_click_pos(self, plugin, name, screen=None, match_center=None):
        """
        计算模板对应的点击坐标（基准）。
        返回 (x, y) 或 (x1,y1,x2,y2) 或 None
        """
        if screen is None:
            screen = self.take_screenshot()
            if screen is None:
                return None
        return self.tm.compute_click_pos(plugin, name, screen, match_center=match_center)

    def match_and_click(self, plugin, name, screen=None):
        """匹配并点击（基准坐标）"""
        if screen is None:
            screen = self.take_screenshot()
            if screen is None:
                return False
        return self.tm.match_and_click(
            screen, plugin, name,
            tap_func=lambda x, y: self.adb_tap(x, y),
            normal_click_func=self.normal_click,
        )

    # ==================================================================
    #  等待
    # ==================================================================

    def wait_for_entry(self, plugin, name, max_wait=5, poll_interval=0.3):
        start = time.time()
        while time.time() - start < max_wait:
            self._wait_if_paused()
            if not self.running or self.tab.task_stop_flag:
                return False
            screen = self.take_screenshot()
            if screen is not None and self.tm.exists(screen, plugin, name):
                return True
            time.sleep(poll_interval)
        return False

    def wait_for_entry_disappear(self, plugin, name, max_wait=5, poll_interval=0.2):
        start = time.time()
        while time.time() - start < max_wait:
            self._wait_if_paused()
            if not self.running or self.tab.task_stop_flag:
                return False
            screen = self.take_screenshot()
            if screen is not None and not self.tm.exists(screen, plugin, name):
                return True
            time.sleep(poll_interval)
        return False

    def wait_entry_and_click(self, plugin, name, max_wait=5):
        if not self.wait_for_entry(plugin, name, max_wait):
            return False
        return self.match_and_click(plugin, name)

    def wait_interface_switch(
        self,
        from_plugin, from_name,
        to_plugin, to_name,
        max_wait=15.0,
        poll_interval=0.25,
    ):
        """
        轮询等待界面从 A 切到 B（双向验证）。

        参数：
            from_plugin, from_name: 起点界面
                                    传 None 表示只验证终点出现（单向）
            to_plugin, to_name:     终点界面
            max_wait:               最长等待秒数
            poll_interval:          轮询间隔秒数

        返回：
            True  → 切换完成
            False → 超时 / 被停止 / 截图持续失败

        判定逻辑：
            每一帧同时检查：
              a_gone = 起点界面不存在
              b_here = 终点界面存在
            同一帧里 a_gone and b_here 同时成立 → True
            只看到一边不算，防止"过渡中"误判。

        用于所有界面切换：点完某个按钮之后，确认画面真的从 A 切到了 B。
        """
        if to_plugin is None or to_name is None:
            return False

        check_from = (from_plugin is not None and from_name is not None)

        start = time.time()
        while time.time() - start < max_wait:
            self._wait_if_paused()
            if not self.running or self.tab.task_stop_flag:
                return False

            screen = self.take_screenshot()
            if screen is None:
                time.sleep(poll_interval)
                continue

            # 先判终点：终点还没出现，就不用查起点了
            try:
                b_here = self.tm.exists(screen, to_plugin, to_name)
            except Exception:
                b_here = False

            if not b_here:
                time.sleep(poll_interval)
                continue

            # 单向模式：终点出现即成功
            if not check_from:
                return True

            # 双向模式：起点必须同时消失
            try:
                a_gone = not self.tm.exists(screen, from_plugin, from_name)
            except Exception:
                a_gone = False

            if a_gone:
                return True

            time.sleep(poll_interval)

        return False

    def click_and_wait_switch(
        self,
        click_fn,
        origin_check_fn,
        target_plugin, target_name,
        max_wait=10.0,
        max_click=3,
        window_ms=(1000, 1200),
        poll_interval=0.1,
        tag='',
    ):
        """
        点按钮 + 轮询等界面切换（带点击重试）。

        模式：
            点一次 → 轮询窗口 window_ms
                窗口内看到目标界面  → True
                窗口结束还在原地    → 再点一次
                窗口结束两个都不在  → 不点，继续轮询（过渡中）
            总超时 max_wait，点击上限 max_click 次

        参数：
            click_fn        无参函数，执行一次点击动作。
                            返回 True 表示点击成功，False 表示无法点击。
            origin_check_fn lambda screen: bool，判定"还在原地"。
                            模板判定或颜色判定都由调用方传入。
            target_plugin / target_name
                            目标界面模板
            max_wait        总超时秒数
            max_click       点击次数上限
            window_ms       (lo, hi)，每次点击后的轮询窗口（毫秒）
            poll_interval   窗口内轮询间隔秒数
            tag             日志前缀（如 '百鬼入口' / '结算'）

        返回：
            True  → 看到目标界面
            False → 超时 / 点击失败 / 被停止
        """
        start = time.time()
        clicked = 0
        need_click = True
        prefix = f'{tag} ' if tag else ''

        while time.time() - start < max_wait:
            self._wait_if_paused()
            if not self.running or self.tab.task_stop_flag:
                return False

            # ---- 点击阶段 ----
            if need_click:
                if clicked >= max_click:
                    self.log(f'{prefix}已点 {max_click} 次仍未切换界面')
                    return False
                try:
                    ok = click_fn()
                except Exception:
                    ok = False
                if not ok:
                    self.log(f'{prefix}点击失败')
                    return False
                clicked += 1
                self.log(f'{prefix}点击第 {clicked} 次', quiet=True)
                need_click = False

            # ---- 轮询窗口 ----
            window = random.randint(window_ms[0], window_ms[1]) / 1000.0
            window_start = time.time()
            while time.time() - window_start < window:
                if not self.running or self.tab.task_stop_flag:
                    return False
                screen = self.take_screenshot()
                if screen is None:
                    time.sleep(poll_interval)
                    continue
                try:
                    if self.tm.exists(screen, target_plugin, target_name):
                        return True
                except Exception:
                    pass
                time.sleep(poll_interval)

            # ---- 窗口结束，判一次 ----
            screen = self.take_screenshot()
            if screen is None:
                continue
            try:
                if self.tm.exists(screen, target_plugin, target_name):
                    return True
                origin_here = origin_check_fn(screen)
            except Exception:
                origin_here = False

            if origin_here:
                need_click = True
            # else: 过渡中，need_click 保持 False，继续轮询

        return False

    # ==================================================================
    #  截图 / 点击 / 滑动（全部基准坐标）
    # ==================================================================

    def take_screenshot(self, retries=3):
        for _ in range(retries):
            self._wait_if_paused()
            screen = take_screenshot_np(self.device)
            if screen is not None:
                return screen
            time.sleep(0.1)
        self.log('❌ 截图失败(连续重试后)')
        return None

    def _wait_if_paused(self):
        self.tab.wait_if_paused()
    
    def enter_state(self, state_name):
        self.tab.enter_state(state_name)
    
    def exit_state(self, state_name):
        self.tab.exit_state(state_name)

    def normal_click(self, region_base):
        """在基准区域内正态选点，返回基准坐标"""
        return normal_click_in_region(region_base)

    def adb_tap(self, x, y):
        """点击基准坐标"""
        self._wait_if_paused()
        device_tap(self.device, x, y)

    def adb_swipe(self, x1, y1, x2, y2, duration):
        """滑动，参数为基准坐标"""
        self._wait_if_paused()
        device_swipe(self.device, x1, y1, x2, y2, duration)

    # ==================================================================
    #  纯坐标 / 滑动（走 source_<base>.json 的 coords / swipes 段）
    # ==================================================================

    def click_coord(self, plugin, name):
        """按配置点击，不依赖模板匹配。返回 True/False"""
        return self.tm.click_coord(
            plugin, name,
            tap_func=lambda x, y: self.adb_tap(x, y),
            normal_click_func=self.normal_click,
        )

    def swipe_coord(self, plugin, name):
        """按配置执行滑动（区域随机 + 节点 + 倾斜 + 抖动）"""
        result = self.tm.compute_swipe_paths(plugin, name)
        if not result:
            return False
        duration = result['duration_ms']
        interval = result.get('interval_ms', 50) / 1000.0
        for sx, sy, ex, ey in result['paths']:
            if not self.running or self.tab.task_stop_flag:
                return False
            self.adb_swipe(sx, sy, ex, ey, duration)
            if interval > 0:
                time.sleep(interval)
        return True

    # ==================================================================
    #  网格（走 source_<base>.json 的 grids 段）
    # ==================================================================

    def grid_scan(self, plugin, name):
        """
        扫描一个网格，返回每个格子的状态。

        返回：
            [{'idx': 0, 'matched': True,  'cell': [x1,y1,x2,y2]}, ...]
            或 None（配置缺失 / 无 color_check）

        判定规则：
            双参照。离 unchallenged_bgr 更近 → matched = True。
            连续 frames 帧都命中才算 matched（默认 2 帧）。
        """
        cfg = self.tm.get_grid(plugin, name)
        if not cfg:
            return None
        cc = cfg.get('color_check')
        if not cc:
            return None

        u_bgr = cc.get('unchallenged_bgr')
        c_bgr = cc.get('challenged_bgr')
        if not u_bgr or not c_bgr:
            self.log(f'❌ {plugin}/{name} 缺少 unchallenged_bgr / challenged_bgr')
            return None

        u_bgr = np.array(u_bgr, dtype=float)
        c_bgr = np.array(c_bgr, dtype=float)
        sample_size = int(cc.get('sample_size', 3))
        frames = max(1, int(cc.get('frames', 2)))

        cells = expand_grid(cfg)
        hits = [0] * len(cells)

        for _ in range(frames):
            screen = self.take_screenshot()
            if screen is None:
                break
            for i, cell in enumerate(cells):
                sx, sy = grid_sample_point(cell, cc)
                avg = self.get_average_color_from_screen(
                    screen, sx, sy, sample_size_base=sample_size)
                if avg is None:
                    continue
                d_u = float(np.linalg.norm(avg - u_bgr))
                d_c = float(np.linalg.norm(avg - c_bgr))
                if d_u < d_c:
                    hits[i] += 1

        results = []
        for i, cell in enumerate(cells):
            results.append({
                'idx': i,
                'matched': hits[i] >= frames,
                'cell': list(cell),
            })
        return results

    def grid_click(self, plugin, name, idx):
        """点击网格中第 idx 个格子。click_mode 由配置决定，返回 True/False"""
        cfg = self.tm.get_grid(plugin, name)
        if not cfg:
            return False
        cells = expand_grid(cfg)
        if idx < 0 or idx >= len(cells):
            return False
        cell = cells[idx]
        mode = cfg.get('click_mode', 'region')
        if mode == 'center':
            # 点击区域 = 格子 cell
            x1, y1, x2, y2 = cell
            cx = (x1 + x2) // 2
            cy = (y1 + y2) // 2
            px, py = gauss_in_rect(cx, cy, abs(x2 - x1), abs(y2 - y1),
                                   sigma_div=6)
            x, y = clamp_rect(px, py, x1, y1, x2, y2)
        else:
            x, y = self.normal_click(cell)
        self.adb_tap(x, y)
        return True

    # ==================================================================
    #  颜色检测（基准坐标）
    # ==================================================================

    def get_average_color_from_screen(self, screen, x0_base, y0_base, sample_size_base=10):
        """采样屏幕某点的平均颜色。x0_base, y0_base 是当前基准坐标"""
        if screen is None:
            return None
        h, w = screen.shape[:2]
        base_w, base_h = self.tm.base_res
        sx = int(x0_base * w / base_w)
        sy = int(y0_base * h / base_h)
        size = max(1, int(sample_size_base * min(w / base_w, h / base_h)))
        x1 = max(0, sx - size // 2)
        y1 = max(0, sy - size // 2)
        x2 = min(w, x1 + size)
        y2 = min(h, y1 + size)
        region = screen[y1:y2, x1:x2]
        if region.size == 0:
            return None
        return np.mean(region.reshape(-1, 3), axis=0)

    def get_region_average_bgr(self, screen, region_base):
        """取任意矩形的平均 BGR。region_base = [x1, y1, x2, y2]（基准坐标）"""
        if screen is None:
            return None
        h, w = screen.shape[:2]
        base_w, base_h = self.tm.base_res
        x1, y1, x2, y2 = region_base
        sx1 = max(0, int(x1 * w / base_w))
        sy1 = max(0, int(y1 * h / base_h))
        sx2 = min(w, int(x2 * w / base_w))
        sy2 = min(h, int(y2 * h / base_h))
        if sx2 <= sx1 or sy2 <= sy1:
            return None
        roi = screen[sy1:sy2, sx1:sx2]
        if roi.size == 0:
            return None
        return np.mean(roi.reshape(-1, 3), axis=0)

    def wait_for_color(self, x_base, y_base, target_color, tolerance=30,
                       max_wait=10, sample_size_base=10, consecutive=2):
        """
        等待某点颜色匹配。

        target_color 必须按 BGR 顺序传入。
        """
        target = np.array(target_color)
        confirm_count = 0
        start = time.time()
        while time.time() - start < max_wait:
            self._wait_if_paused()
            if not self.running or self.tab.task_stop_flag:
                return False
            screen = self.take_screenshot()
            if screen is None:
                continue
            avg = self.get_average_color_from_screen(screen, x_base, y_base, sample_size_base)
            if avg is not None and np.linalg.norm(avg - target) < tolerance:
                confirm_count += 1
                if confirm_count >= consecutive:
                    return True
            else:
                confirm_count = 0
            time.sleep(0.2)
        return False