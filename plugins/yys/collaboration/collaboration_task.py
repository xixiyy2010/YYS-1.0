# plugins/yys/collaboration/collaboration_task.py
import time
import random

import numpy as np

from plugins.yys._shared.task_base import BaseTask
from plugins.yys._shared.navigation_engine import Navigator
from plugins.yys._shared.resources import StateNames


class CollaborationTaskThread(BaseTask):
    """
    探索 困28 + 个人突破联动插件（网格驱动）

    改动要点（v2）：
        - 6 处界面切换改用 click_and_wait_switch：
            wait_for_c28_ready / enter_dungeon_from_ready /
            enter_tupo_from_tansuo / exit_c28_and_goto_tupo /
            exit_tupo / refresh_tupo_grid
        - _scan_grid_states 采样方式修正：
            改用 get_average_color_from_screen（点采样 + 尺寸自动缩放）。
            原来用 get_region_average_bgr（矩形不缩放），窗口分辨率下
            采样区缩到 0 宽返回 None，导致九宫格全判 unknown。
    """

    # ---------- 超时（秒）----------
    TIMEOUT_WAIT_TEMPLATE = 15         # 等模板出现
    TIMEOUT_WAIT_BATTLE_END = 180      # 等战斗结束（1~3 分钟）
    TIMEOUT_WAIT_RESIDUE_CLEAR = 15    # 等宝箱残留消失
    MAX_C28_SWIPE = 4
    C28_SWIPE_POLL_FAST = 0.6    # 前几次滑动后的轮询窗口（秒）
    C28_SWIPE_POLL_LAST = 6.0    # 最后一次滑动后的轮询窗口（秒）
    C28_SWIPE_POLL_INTERVAL_MS = 100   # 轮询间隔

    def __init__(self, tab, log_callback=None, stats_callback=None, error_callback=None):
        super().__init__(tab, log_callback=log_callback,
                         stats_callback=stats_callback, error_callback=error_callback)

        self.navigator = Navigator(
            self.device,
            log_callback=self.log,
            stop_check_callback=lambda: self.tab.task_stop_flag,
        )

        # 九宫格判定阈值：由 JSON 提供，缺失则报错跳过
        self._grid_rb_threshold = None
        self._grid_bright_threshold = None

        # 统计
        self.tupo_count = 0
        self.fuben_count = 0
        self.xiaoguai_count = 0

    # ==================================================================
    #  工具
    # ==================================================================

    def _sleep(self, lo_ms, hi_ms):
        time.sleep(random.randint(lo_ms, hi_ms) / 1000.0)

    def _stopped(self):
        return (not self.running) or self.tab.task_stop_flag

    # ==================================================================
    #  主循环
    # ==================================================================

    def run(self):
        self.running = True
        self.log('📜 探索绘卷运行中...')
        while self.running and not self.tab.task_stop_flag:
            try:
                if not self.dispatch_by_current_interface():
                    while self.running and not self.tab.task_stop_flag:
                        if self.navigator.go_to_tansuo():
                            break
                        self._sleep(800, 1200)
                    if self._stopped():
                        break
                    continue
            except Exception as e:
                import traceback
                self.log(f'❌ 主循环异常: {e}\n{traceback.format_exc()}')
                self._sleep(1000, 1500)
            self._sleep(80, 120)
        self.running = False

    # ==================================================================
    #  界面分发
    # ==================================================================

    def dispatch_by_current_interface(self):
        """
        检测当前界面，分发到对应阶段。
        返回 True 表示"我处理了当前界面"，False 表示"不认识，交给导航"。
        """
        screen = self.take_screenshot()
        if screen is None:
            return False

        # 1. 个人突破界面
        if self.exists('collaboration', 'gerentupo', screen=screen):
            return self.tupo_phase(screen)

        # 2. 探索界面
        if self.exists('common', 'tansuo', screen=screen):
            return self.tansuo_phase_from_tansuo(screen)

        # 3. 困28 准备界面
        if self.exists('collaboration', 'C28ZB', screen=screen):
            return self.tansuo_phase_from_ready(screen)

        # 4. 副本内
        if self.exists('collaboration', 'fuben', screen=screen):
            return self.tansuo_phase_inside(screen)

        return False

    # ==================================================================
    #  探索界面阶段
    # ==================================================================

    def tansuo_phase_from_ready(self, screen):
        """
        当前在 困28 准备界面。
        - 检测 C28ZBtupojuan → 满了 → 点 tuichu 退出 → 探索 → 个人突破
        - 没检测到 → 点进入副本按钮，进副本
        """
        if self.exists('collaboration', 'C28ZBtupojuan', screen=screen):
            self.log('🔍 困28准备:券已满,去突破')
            return self.exit_c28_and_goto_tupo()
        else:
            self.log('🔍 困28准备:券未满,继续')
            return self.enter_dungeon_from_ready(screen)

    def tansuo_phase_inside(self, screen=None):
        """
        副本内主循环。打完一局 continue，回到循环开头重新检测。
        只有券满或出问题才 return。
        """
        while self.running and not self.tab.task_stop_flag:
            screen = self.take_screenshot()
            if screen is None:
                return False

            # 副本内 → 打怪
            if self.exists('collaboration', 'fuben', screen=screen):
                if not self.fight_in_dungeon():
                    return False
                # 走到这里 = 打完BOSS、出副本
                self.fuben_count += 1
                self.log(f'📢 统计:结界:{self.tupo_count} 探索:{self.fuben_count}')
                self.post_boss_phase()
                continue

            # 探索界面
            if self.exists('common', 'tansuo', screen=screen):
                if self.tansuo_phase_from_tansuo(screen):
                    return True
                else:
                    return False

            # 困28准备界面
            if self.exists('collaboration', 'C28ZB', screen=screen):
                if self.tansuo_phase_from_ready(screen):
                    return True
                else:
                    return False

            return False

        return True

    # ==================================================================
    #  界面切换
    # ==================================================================

    def tansuo_phase_from_tansuo(self, screen=None):
        fresh = self.take_screenshot()
        if fresh is None:
            return False

        if self.exists('collaboration', 'tansuotupojuan', screen=fresh):
            self.log('🔍 探索界面:券已满,去突破')
            return self.enter_tupo_from_tansuo(fresh)
        else:
            self.log('🔍 探索界面:券未满,继续')
            return self.wait_for_c28_ready()

    # ==================================================================
    #  打完 BOSS 后：从出副本到下次进副本的专属流程
    # ==================================================================

    def post_boss_phase(self):
        """打完BOSS后，从出副本到下次进副本这一段（只有打完BOSS才调）"""
        screen = self.take_screenshot()
        if screen is None:
            return False

        # 已经是准备界面 → 直接走准备界面流程
        if self.exists('collaboration', 'C28ZB', screen=screen):
            self.log('🏁 准备界面已出现')
            return self.tansuo_phase_from_ready(screen)

        # 是探索界面 → 看券满不满
        if self.exists('common', 'tansuo', screen=screen):
            if self.exists('collaboration', 'tansuotupojuan', screen=screen):
                self.log('🔍 探索界面:券已满,去突破')
                return self.enter_tupo_from_tansuo(screen)
            self.log('🔍 探索界面:券未满,继续')
            return self.boss_observe_then_c28(screen)

        # 其它界面 → 交给主循环重新判断
        return False

    def boss_observe_then_c28(self, screen):
        """打完BOSS后看到探索界面，先观察 0.8~1.2 秒，看准备界面会不会自己弹出"""
        wait_time = random.uniform(0.8, 1.2)
        self.log(f'👀 观察 {wait_time:.2f} 秒')
        start = time.time()
        last = screen

        while time.time() - start < wait_time:
            if self._stopped():
                return False
            self._sleep(80, 120)
            s = self.take_screenshot()
            if s is None:
                continue
            last = s
            # 优先判准备界面
            if self.exists('collaboration', 'C28ZB', screen=s):
                self.log('🏁 准备界面已弹出')
                return self.tansuo_phase_from_ready(s)

        # 观察结束，最后是探索界面 → 走原来的找 C28 流程
        if self.exists('common', 'tansuo', screen=last):
            return self.wait_for_c28_ready()
        return False

    def wait_for_c28_ready(self, timeout=15):
        start = time.time()
        while time.time() - start < timeout:
            if self._stopped():
                return False
            screen = self.take_screenshot()
            if screen is None:
                continue

            if self.exists('collaboration', 'C28ZB', screen=screen):
                self.log('🏁 困28准备界面已出现')
                return self.tansuo_phase_from_ready(screen)

            if self.exists('common', 'tansuo', screen=screen):
                # 必须先检测到 困28入口，才允许点固定坐标
                if not self.ensure_c28_visible(screen):
                    self.log('❌ 未检测到困28入口，放弃点击')
                    return False

                self.log('👉 点击困28入口')
                if not self.click_and_wait_switch(
                        click_fn=lambda: self.click_coord(
                            'collaboration', 'c28rukou'),
                        origin_check_fn=lambda s: self.tm.exists(
                            s, 'common', 'tansuo'),
                        target_plugin='collaboration', target_name='C28ZB',
                        max_wait=self.TIMEOUT_WAIT_TEMPLATE,
                        max_click=3,
                        tag='困28入口'):
                    return False
                return True

            self._sleep(80, 120)

        self.log('❌ 等待困28准备界面超时')
        return False

    def _wait_for_c28(self, timeout):
        """在 timeout 秒内轮询检测 collaboration/C28，命中返回 True"""
        start = time.time()
        while time.time() - start < timeout:
            if self._stopped():
                return False
            screen = self.take_screenshot()
            if screen is not None and self.exists('collaboration', 'C28', screen=screen):
                return True
            self._sleep(self.C28_SWIPE_POLL_INTERVAL_MS, self.C28_SWIPE_POLL_INTERVAL_MS)
        return False

    def ensure_c28_visible(self, screen=None, max_swipes=None):
        if max_swipes is None:
            max_swipes = self.MAX_C28_SWIPE

        if screen is None:
            screen = self.take_screenshot()
        if screen is not None and self.exists('collaboration', 'C28', screen=screen):
            return True

        for i in range(max_swipes):
            if self._stopped():
                return False

            is_last = (i == max_swipes - 1)
            self.log(f'↕️ 滑动 {i+1}/{max_swipes} 找困28入口')
            self.swipe_coord('collaboration', 'c28rukouhuadong')

            # 前几次滑动后不检测，最后一次才检测
            if is_last:
                if self._wait_for_c28(self.C28_SWIPE_POLL_LAST):
                    self.log(f'🏁 第 {i + 1} 次滑动后检测到困28入口')
                    return True

        self.log(f'❌ 滑动{max_swipes}次仍未检测到困28入口')
        return False

    def enter_dungeon_from_ready(self, screen=None):
        """困28 准备界面 → 点进入副本 → 等切到 fuben"""
        return self.click_and_wait_switch(
            click_fn=lambda: self.click_coord(
                'collaboration', 'c28tansuo'),
            origin_check_fn=lambda s: self.tm.exists(
                s, 'collaboration', 'C28ZB'),
            target_plugin='collaboration', target_name='fuben',
            max_wait=self.TIMEOUT_WAIT_TEMPLATE,
            max_click=3,
            tag='进入副本')

    def enter_tupo_from_tansuo(self, screen=None):
        """探索界面 → 点结界突破入口 → 等切到 gerentupo"""
        if not self.click_and_wait_switch(
                click_fn=lambda: self.match_and_click(
                    'common', 'jiejietupo'),
                origin_check_fn=lambda s: self.tm.exists(
                    s, 'common', 'tansuo'),
                target_plugin='collaboration', target_name='gerentupo',
                max_wait=self.TIMEOUT_WAIT_TEMPLATE,
                max_click=3,
                tag='结界突破'):
            self.log('❌ 进入个人突破失败')
            return False
        return True

    def exit_c28_and_goto_tupo(self):
        """
        困28 准备界面突破券已满：
          1. 点击固定坐标 common/tuichu（返回上一级）
          2. 等回到探索界面
          3. 从探索界面进入个人突破
        """
        if not self.click_and_wait_switch(
                click_fn=lambda: self.click_coord(
                    'common', 'tuichu'),
                origin_check_fn=lambda s: self.tm.exists(
                    s, 'collaboration', 'C28ZB'),
                target_plugin='common', target_name='tansuo',
                max_wait=self.TIMEOUT_WAIT_TEMPLATE,
                max_click=3,
                tag='退出困28'):
            self.log('❌ 点击退出后等待探索界面超时')
            return False
        self.log('🏁 已回到探索界面')

        return self.enter_tupo_from_tansuo()

    # ==================================================================
    #  个人突破阶段
    # ==================================================================

    def tupo_phase(self, screen=None):
        while self.running and not self.tab.task_stop_flag:
            # 1. 等宝箱残留消失（颜色正常 = 在个人突破界面）
            if not self.wait_for_residue_clear():
                return False

            # 2. 截图
            screen = self.take_screenshot()
            if screen is None:
                return False

            # 3. 检测券=0
            if self.exists('collaboration', 'gerentupojuan', screen=screen):
                self.log('🔍 个人突破:券清完,去探索')
                return self.exit_tupo()

            # 4. 检测九宫格
            unchallenged, failed = self.get_grid_states()

            if not unchallenged:
                if failed:
                    self.log(f'🔍 个人突破:失败{len(failed)},刷新')
                    if not self.refresh_tupo_grid():
                        return False
                    continue
                self.log('未检测到可打的格子，等一下再试')
                self._sleep(500, 800)
                continue

            # 5. 打九宫格
            if len(unchallenged) == 1:
                idx = unchallenged[0]
                self.log(f'剩余 1 个格子（{idx+1}），打 8 退 2')
                if not self.handle_last_grid_special(idx):
                    return False
            else:
                idx = random.choice(unchallenged)
                self.log(f'选择格子 {idx+1}')
                if not self.fight_one_grid(idx):
                    return False

            self.tupo_count += 1
            self.log(f'📢 统计:结界:{self.tupo_count} 探索:{self.fuben_count}')

        return True

    # ==================================================================
    #  九宫格状态扫描（三态）
    # ==================================================================

    def _classify_grid_color(self, bgr):
        """根据 BGR 判三态：
           失败     ：R - B > 阈值（暖色）
           未打     ：非失败 且 亮度 > 阈值
           已打     ：其余
        """
        if bgr is None:
            return 'unknown'
        b, g, r = int(bgr[0]), int(bgr[1]), int(bgr[2])
        rb_diff = r - b
        bright = (r + g + b) / 3.0

        if rb_diff > self._grid_rb_threshold:
            return 'failed'
        if bright > self._grid_bright_threshold:
            return 'unchallenged'
        return 'challenged'

    def _scan_grid_states(self, screen=None):
        """扫描 collaboration/格子，返回 [(idx, state), ...]，idx 从 0 开始"""
        if screen is None:
            screen = self.take_screenshot()
        if screen is None:
            return []

        cfg = self.tm.get_grid('collaboration', '格子')
        if not cfg:
            self.log('❌ 缺少格子配置')
            return []

        cc = cfg.get('color_check') or {}

        required = ('corner', 'offset_x', 'offset_y', 'sample_size',
                    'rb_diff_threshold', 'brightness_threshold')
        missing = [k for k in required if k not in cc]
        if missing:
            self.log(f'❌ 格子配置缺少字段: {", ".join(missing)}')
            return []

        corner = cc['corner']
        off_x = int(cc['offset_x'])
        off_y = int(cc['offset_y'])
        sample_size = int(cc['sample_size'])
        self._grid_rb_threshold = int(cc['rb_diff_threshold'])
        self._grid_bright_threshold = float(cc['brightness_threshold'])

        x_start = int(cfg['x_start'])
        y_start = int(cfg['y_start'])
        w = int(cfg['w'])
        h = int(cfg['h'])
        gap_x = int(cfg.get('gap_x', 0))
        gap_y = int(cfg.get('gap_y', 0))
        cols = int(cfg['cols'])
        rows = int(cfg['rows'])

        half = sample_size // 2
        results = []
        for rr in range(rows):
            for cc_i in range(cols):
                idx = rr * cols + cc_i
                cx = x_start + cc_i * (w + gap_x)
                cy = y_start + rr * (h + gap_y)

                if corner == 'top_left':
                    px, py = cx + off_x, cy + off_y
                elif corner == 'top_right':
                    px, py = cx + w + off_x, cy + off_y
                elif corner == 'bottom_left':
                    px, py = cx + off_x, cy + h + off_y
                elif corner == 'bottom_right':
                    px, py = cx + w + off_x, cy + h + off_y
                elif corner == 'center':
                    px, py = cx + w // 2 + off_x, cy + h // 2 + off_y
                else:
                    px, py = cx + off_x, cy + off_y

                # 用 get_average_color_from_screen（点采样）：
                # 它会按 screen/base 比例缩放采样尺寸，窗口分辨率下
                # 采样区不会缩到 0 宽（get_region_average_bgr 会）。
                avg = self.get_average_color_from_screen(
                    screen, px, py, sample_size_base=sample_size)
                state = self._classify_grid_color(avg)
                results.append((idx, state))

        return results

    def get_grid_states(self):
        """返回 (unchallenged, failed) 两个索引列表"""
        states = self._scan_grid_states()
        unchallenged = [i for i, s in states if s == 'unchallenged']
        failed = [i for i, s in states if s == 'failed']
        return unchallenged, failed

    def exit_tupo(self):
        """个人突破界面 → 点结界退出 → 等回到探索"""
        return self.click_and_wait_switch(
            click_fn=lambda: self.match_and_click(
                'collaboration', 'jiejietuichu'),
            origin_check_fn=lambda s: self.tm.exists(
                s, 'collaboration', 'gerentupo'),
            target_plugin='common', target_name='tansuo',
            max_wait=self.TIMEOUT_WAIT_TEMPLATE,
            max_click=3,
            tag='退出个人突破')

    def refresh_tupo_grid(self, timeout=15):
        """点击个人突破刷新按钮，等出现未打格子"""
        # 第 1 步：点 shuaxin → 等 queding 出现（带点击重试）
        if not self.click_and_wait_switch(
                click_fn=lambda: self.match_and_click(
                    'collaboration', 'shuaxin'),
                origin_check_fn=lambda s: self.tm.exists(
                    s, 'collaboration', 'gerentupo'),
                target_plugin='common', target_name='queding',
                max_wait=self.TIMEOUT_WAIT_TEMPLATE,
                max_click=3,
                tag='刷新'):
            self.log('❌ 等刷新确定按钮超时')
            return False
        self.log('👉 点击个人突破刷新')

        # 第 2 步：点 queding
        if not self.match_and_click('common', 'queding'):
            self.log('❌ 未找到刷新确定按钮')
            return False
        self.log('👉 点击刷新确定')

        # 第 3 步：等刷新动画 + 等未打格子出现（颜色检测，不是模板）
        start = time.time()
        while time.time() - start < timeout:
            if self._stopped():
                return False
            self._sleep(400, 600)
            screen = self.take_screenshot()
            if screen is None:
                continue
            states = self._scan_grid_states(screen)
            unchallenged = [i for i, s in states if s == 'unchallenged']
            if unchallenged:
                self.log(f'🏁 刷新完成，检测到 {len(unchallenged)} 个未打格子')
                return True

        self.log('❌ 刷新后未检测到未打格子，超时')
        return False

    # ==================================================================
    #  单个格子：正常打
    # ==================================================================

    def fight_one_grid(self, idx):
        """点格子 → 调进战斗检测（内含点进攻）→ 等战斗结束"""
        self.grid_click('collaboration', '格子', idx)
        self._sleep(250, 350)

        if not self._wait_enter_battle_tupo(idx):
            self.log('❌ 点进攻后未进战斗')
            return False

        return self.wait_for_battle_end()

    def wait_for_battle_end(self, timeout=240, return_timeout=30):
        """突破战斗结束：
             1. 等 zhandoutuichu 消失（默认 240 秒）
             2. 等 2 秒跳过结算
             3. 等 gerentupo 回来（默认 30 秒）
        """
        if not self._wait_battle_end(timeout):
            return False
        self._wait_skip()
        return self._wait_return(
            'collaboration', 'gerentupo', timeout=return_timeout)

    # ==================================================================
    #  单个格子：打 8 退 2
    # ==================================================================

    def handle_last_grid_special(self, idx):
        """
        打 8 退 2：
            升维点：点最后一个格子 + 点进攻（打出第一枪）
            第 1 回合：等退出 → 点退出 → 点确认 → 点重新挑战
            第 2 回合：等退出 → 点退出 → 点确认 → 点重新挑战
            降维点：第 3 次确认进入战斗之后
            第 3 次：正常打完
        """
        # ---- 点格子 + 点进攻（打出第一枪）----
        self.grid_click('collaboration', '格子', idx)
        self._sleep(250, 350)
        self.grid_click('collaboration', '进攻', idx)

        # ---- 升维 ----
        self.enter_state(StateNames.TUPO_8_2)
        try:
            # 第 1 回合：等战斗退出按钮 → 退出 → 重新挑战
            self.log('⏯️ 打 8 退 2 第 1 回合')
            if not self.wait_for_entry('collaboration', 'zhandoutuichu',
                    max_wait=self.TIMEOUT_WAIT_TEMPLATE, poll_interval=0.1):
                self.log('❌ 第 1 回合等战斗界面超时')
                return False
            if not self.exit_battle_and_confirm():
                return False
            if not self.click_retry_button():
                return False

            # 第 2 回合：等战斗退出按钮 → 退出 → 重新挑战（= 第 3 次打的开始）
            self.log('⏯️ 打 8 退 2 第 2 回合')
            if not self.wait_for_entry('collaboration', 'zhandoutuichu',
                    max_wait=self.TIMEOUT_WAIT_TEMPLATE, poll_interval=0.1):
                self.log('❌ 第 2 回合等战斗界面超时')
                return False
            if not self.exit_battle_and_confirm():
                return False
            if not self.click_retry_button():
                return False

            # 第 3 次：等确认进入战斗，再降维
            self.log('⏯️ 打 8 退 2 第 3 次等进战斗')
            if not self.wait_for_entry('collaboration', 'zhandoutuichu',
                    max_wait=self.TIMEOUT_WAIT_TEMPLATE, poll_interval=0.1):
                self.log('❌ 第 3 次等战斗界面超时')
                return False
        finally:
            # ---- 降维 ----
            self.exit_state(StateNames.TUPO_8_2)

        # 第 3 次正常打完
        self.log('⏯️ 打 8 退 2 第 3 次（正常打）')
        return self.wait_for_battle_end()

    def enter_battle_and_exit(self, idx):
        self.grid_click('collaboration', '格子', idx)
        self._sleep(250, 350)

        self.grid_click('collaboration', '进攻', idx)

        if not self.wait_for_entry(
                'collaboration', 'zhandoutuichu',
                max_wait=self.TIMEOUT_WAIT_TEMPLATE,
                poll_interval=0.1):
            self.log('❌ 进战斗超时')
            return False

        return self.exit_battle_and_confirm()

    def exit_battle_and_confirm(self):
        screen = self.take_screenshot()
        if screen is None:
            return False
        if not self.match_and_click('collaboration', 'zhandoutuichu', screen=screen):
            self.log('❌ 未找到战斗退出按钮')
            return False

        if not self.wait_for_entry(
                'collaboration', 'zhandoutuichuqueren',
                max_wait=10, poll_interval=0.1):
            self.log('❌ 等退出确认超时')
            return False

        screen = self.take_screenshot()
        if screen is None:
            return False
        if not self.match_and_click('collaboration', 'zhandoutuichuqueren', screen=screen):
            self.log('❌ 未找到退出确认')
            return False
        return True

    def click_retry_button(self):
        if not self.wait_for_entry(
                'collaboration', 'zaicitiaozhan',
                max_wait=self.TIMEOUT_WAIT_TEMPLATE,
                poll_interval=0.1):
            self.log('❌ 等再次挑战超时')
            return False

        screen = self.take_screenshot()
        if screen is None:
            return False
        if not self.match_and_click('collaboration', 'zaicitiaozhan', screen=screen):
            self.log('❌ 未找到再次挑战按钮')
            return False

        start = time.time()
        while time.time() - start < 8:
            if self._stopped():
                return False
            screen = self.take_screenshot()
            if screen is None:
                continue

            if self.exists('collaboration', 'zhandoutuichu', screen=screen):
                return True

            if self.exists('collaboration', 'zaicitiaozhandagou', screen=screen):
                self.match_and_click('collaboration', 'zaicitiaozhandagou', screen=screen)
                self._sleep(300, 500)
                continue

            if self.exists('common', 'queding', screen=screen):
                self.match_and_click('common', 'queding', screen=screen)
                return True

            self._sleep(100, 150)

        return False

    # ==================================================================
    #  等宝箱残留消失（纯颜色检测，独立于网格）
    # ==================================================================

    def wait_for_residue_clear(self, timeout=None):
        """
        等宝箱残留消失：区域颜色连续 N 帧匹配"正常色"。
        配置来源：regions/collaboration/jiangli（纯检测，跟网格无关）
        """
        if timeout is None:
            timeout = self.TIMEOUT_WAIT_RESIDUE_CLEAR

        cfg = self.tm.get_region('collaboration', 'jiangli')
        if not cfg:
            self.log('❌ 缺少奖励配置')
            return False

        region = cfg.get('region')
        normal_bgr = cfg.get('normal_bgr')
        abnormal_bgr = cfg.get('abnormal_bgr')
        frames_need = cfg.get('frames')

        if not region or len(region) != 4:
            self.log('❌ 奖励格式错误')
            return False
        if not normal_bgr or not abnormal_bgr:
            self.log('❌ 奖励缺少颜色配置')
            return False
        if not frames_need or frames_need <= 0:
            self.log('❌ 奖励帧数无效')
            return False

        normal_bgr = np.array(normal_bgr)
        abnormal_bgr = np.array(abnormal_bgr)
        frames_need = int(frames_need)

        start = time.time()
        consecutive = 0
        while time.time() - start < timeout:
            if self._stopped():
                return False
            screen = self.take_screenshot()
            if screen is None:
                continue
            avg = self.get_region_average_bgr(screen, region)
            if avg is None:
                continue

            d_normal = np.linalg.norm(avg - normal_bgr)
            d_abnormal = np.linalg.norm(avg - abnormal_bgr)

            if d_normal < d_abnormal:
                consecutive += 1
                if consecutive >= frames_need:
                    return True
            else:
                consecutive = 0

            self._sleep(30, 30)

        self.log('❌ 等待宝箱残留消失超时')
        return False

    # ==================================================================
    #  副本内打怪
    # ==================================================================

    def fight_in_dungeon(self):
        swipe_count = 0
        consecutive_failures = 0
        dungeon_xiaoguai_count = 0
        no_swipe_start_time = None

        while self.running and not self.tab.task_stop_flag:
            self._sleep(150, 250)
            screen = self.take_screenshot()
            if screen is None:
                continue

            target = self._find_dungeon_target(screen)
            if target is not None:
                kind, pt = target
                result = self._process_clicked_target(kind, pt)

                if result == 'failed':
                    consecutive_failures += 1
                    if consecutive_failures >= 3:
                        self.log('❌ 连续 3 次点击未进入战斗')
                        return False
                    continue

                if result == 'error':
                    return False

                # ---- 成功处理 ----
                consecutive_failures = 0
                swipe_count = 0
                if result == 'xiaoguai_done':
                    dungeon_xiaoguai_count += 1
                    continue
                elif result == 'boss_done':
                    return True

            if dungeon_xiaoguai_count >= 7:
                if no_swipe_start_time is None:
                    no_swipe_start_time = time.time()
                elif time.time() - no_swipe_start_time > 120:
                    self.log('❌ 等待 BOSS 超时')
                    return False
                self._sleep(800, 1200)
                continue

            swipe_count += 1
            if swipe_count > 3:
                self.log('❌ 滑动超过 3 次仍未找到目标')
                return False

            self.swipe_coord('collaboration', 'c28huadong')
            self._sleep(800, 1200)

            screen2 = self.take_screenshot()
            if screen2 is None:
                continue
            target2 = self._find_dungeon_target(screen2)
            if target2 is not None:
                kind2, pt2 = target2
                result2 = self._process_clicked_target(kind2, pt2)

                if result2 == 'failed':
                    consecutive_failures += 1
                    if consecutive_failures >= 3:
                        self.log('❌ 连续 3 次点击未进入战斗')
                        return False
                    continue

                if result2 == 'error':
                    return False

                # ---- 成功处理 ----
                consecutive_failures = 0
                swipe_count = 0
                if result2 == 'xiaoguai_done':
                    dungeon_xiaoguai_count += 1
                    continue
                elif result2 == 'boss_done':
                    return True

        return False

    def _process_clicked_target(self, kind, pt):
        """点完怪后的通用处理（点击动作在 _wait_enter_battle_dungeon 内部完成）"""
        state = self._wait_enter_battle_dungeon(kind, pt, max_click=3)
        if state != 'battle':
            return 'failed'

        if kind == 'boss':
            self.log('⚔️ 进攻 BOSS')
            if self.handle_boss_battle_end():
                return 'boss_done'
            return 'error'
        else:
            self.xiaoguai_count += 1
            self.log(f'🗡️ 进攻 小怪*{self.xiaoguai_count}')
            if self.wait_for_dungeon_battle_end():
                return 'xiaoguai_done'
            return 'error'

    def _wait_enter_battle_tupo(self, idx, max_click=3):
        """突破进战斗检测：两模板互相验证，最多点 max_click 次进攻
           战斗界面出现 → 成功
           窗口结束 + 突破界面还在 → 再点一次
           窗口结束 + 两个都没 → 不点，继续轮询
        """
        clicked = 0
        need_click = True

        while self.running and not self.tab.task_stop_flag:
            if need_click:
                if clicked >= max_click:
                    self.log(f'❌ 点击进攻 {max_click} 次仍未进战斗')
                    return False
                self.grid_click('collaboration', '进攻', idx)
                clicked += 1
                self.log(f'点击进攻第 {clicked} 次', quiet=True)
                need_click = False

            # 轮询窗口：1~1.2 秒随机
            duration = random.randint(1000, 1200) / 1000.0
            start = time.time()
            while time.time() - start < duration:
                if self._stopped():
                    return False
                screen = self.take_screenshot()
                if screen is None:
                    self._sleep(80, 120)
                    continue
                if self.exists('collaboration', 'zhandoutuichu', screen=screen):
                    return True
                self._sleep(80, 120)

            # 窗口结束，判一次
            screen = self.take_screenshot()
            if screen is None:
                continue
            if self.exists('collaboration', 'zhandoutuichu', screen=screen):
                return True
            if self.exists('collaboration', 'gerentupo', screen=screen):
                # 突破界面还在 → 需要重新点
                need_click = True
                continue
            # 两个都没 → 不点，继续轮询

        return False

    def _wait_enter_battle_dungeon(self, kind, pt, max_click=3):
        """副本进战斗检测（双重验证 + 超时重试）：
          点击目标 → 轮询 1~1.2 秒窗口
            看到 zhandoutuichu → 进战斗，返回 'battle'
            窗口结束：
              - 还在 fuben   → 再点一次（最多 max_click 次）
              - 两个都没有   → 不点，继续轮询（转场中）
            点满 max_click 次仍未进战斗 → 返回 'failed'
        """
        clicked = 0
        need_click = True

        while self.running and not self.tab.task_stop_flag:
            if need_click:
                if clicked >= max_click:
                    self.log(f'❌ 点击 {kind} {max_click} 次仍未进战斗')
                    return 'failed'
                self._click_dungeon_target(pt)
                clicked += 1
                self.log(f'点击 {kind} 第 {clicked} 次', quiet=True)
                need_click = False

            # 轮询窗口：1~1.2 秒随机
            duration = random.randint(1000, 1200) / 1000.0
            start = time.time()
            while time.time() - start < duration:
                if self._stopped():
                    return 'failed'
                screen = self.take_screenshot()
                if screen is None:
                    self._sleep(80, 120)
                    continue
                if self.exists('collaboration', 'zhandoutuichu', screen=screen):
                    return 'battle'
                self._sleep(80, 120)

            # 窗口结束，判一次
            screen = self.take_screenshot()
            if screen is None:
                continue
            if self.exists('collaboration', 'zhandoutuichu', screen=screen):
                return 'battle'
            if self.exists('collaboration', 'fuben', screen=screen):
                # 还在副本界面 → 需要再点一次
                need_click = True
                continue
            # 两个都没 → 不点，继续轮询

        return 'failed'

    def _wait_battle_end(self, timeout=30):
        """等 zhandoutuichu 消失（= 战斗结束）"""
        start = time.time()
        while time.time() - start < timeout:
            if self._stopped():
                return False
            screen = self.take_screenshot()
            if screen is None:
                self._sleep(150, 250)
                continue
            if not self.exists('collaboration', 'zhandoutuichu', screen=screen):
                return True
            self._sleep(150, 250)
        self.log('❌ 等战斗结束超时')
        return False

    def _wait_skip(self, duration=2.0):
        """等跳过结算：读约 2 秒（不 sleep，按时间推进）"""
        start = time.time()
        while time.time() - start < duration:
            if self._stopped():
                return
            self._sleep(80, 120)

    def _wait_return(self, plugin, name, timeout=15):
        """等某界面回来"""
        start = time.time()
        while time.time() - start < timeout:
            if self._stopped():
                return False
            screen = self.take_screenshot()
            if screen is None:
                self._sleep(150, 250)
                continue
            if self.exists(plugin, name, screen=screen):
                return True
            self._sleep(150, 250)
        self.log('❌ 等待界面超时')
        return False

    # ==================================================================
    #  找怪 / 点怪
    # ==================================================================

    def _find_dungeon_target(self, screen):
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=2) as ex:
            fb = ex.submit(self.match, 'collaboration', 'boss', screen)
            fx = ex.submit(self.match, 'collaboration', 'xiaoguai', screen)
            boss_pt = fb.result()
            xiaoguai_pt = fx.result()

        if boss_pt is not None:
            return ('boss', boss_pt)
        if xiaoguai_pt is not None:
            return ('xiaoguai', xiaoguai_pt)
        return None

    def _click_dungeon_target(self, pt):
        self.adb_tap(pt[0], pt[1])

    # ==================================================================
    #  副本内战斗结束
    # ==================================================================

    def wait_for_dungeon_battle_end(self, timeout=30):
        """小怪战斗结束：
             1. 等 zhandoutuichu 消失（timeout 秒）
             2. 等 2 秒跳过结算
             3. 等 fuben 回来（15 秒）
        """
        if not self._wait_battle_end(timeout):
            return False
        self._wait_skip()
        return self._wait_return('collaboration', 'fuben', timeout=15)

    # ==================================================================
    #  打完 BOSS 后：等出副本 + 清宝箱
    # ==================================================================

    def handle_boss_battle_end(self):
        # 1. 等战斗结束
        if not self._wait_battle_end(30):
            return False
        # 2. 等 2 秒跳过结算
        self._wait_skip()
        # 3. 走三分支（探索 / 准备 / C28ZB）
        for round_num in range(2):
            start = time.time()
            while time.time() - start < 15:
                if not self.running or self.tab.task_stop_flag:
                    return False
                screen = self.take_screenshot()
                if screen is None:
                    continue

                if self.exists('common', 'tansuo', screen=screen):
                    self.log('🏁 已出副本，回到探索界面')
                    return True
                if self.exists('collaboration', 'C28ZB', screen=screen):
                    self.log('🏁 已出副本，回到困28准备界面')
                    return True

                if self.exists('collaboration', 'fuben', screen=screen):
                    if self.match_and_click('collaboration', 'baoxiang', screen=screen):
                        self.log('🎁 打开宝箱')
                        time.sleep(0.3)
                        self.click_coord('collaboration', 'baoxiangguanbi')
                        time.sleep(0.5)
                        continue
                    else:
                        time.sleep(0.1)
                        continue

                time.sleep(0.1)

            self.log(f'❌ 出副本第 {round_num+1} 轮超时')

        self.log('❌ 两轮都没出副本，交给导航')
        return False