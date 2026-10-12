# plugins/yys/baigui/baigui_task.py
import time
import random

from plugins.yys._shared.task_base import BaseTask
from plugins.yys._shared.navigation_engine import Navigator


class BaiguiTaskThread(BaseTask):
    """
    百鬼夜行单人插件（坐标走 index.json 的 coords/baigui）

    改动要点（v2）：
        - 4 处界面切换改用 click_and_wait_switch（带点击重试 + 双向验证）：
            町中 → 准备 / 准备 → 三选一 / 三选一 → 砸豆 / 结算 → 准备
        - handle_settlement 重构：
            达标检查提前 → 避免白打一轮
            +1 移到双向验证通过之后 → 修掉"点结算失败也 +1"的 bug
    """

    # ---------- 砸豆子阶段参数（时间，不是坐标） ----------
    THROW_START_DELAY = (2.0, 3.0)
    THROW_PHASE_DURATION = 3.0

    # 三选一候选项名（随机抽一个）
    SANXUANYI_NAMES = ['zuo', 'zhong', 'you']

    def __init__(self, tab, log_callback=None, stats_callback=None, error_callback=None):
        super().__init__(tab, log_callback=log_callback,
                         stats_callback=stats_callback, error_callback=error_callback)

        self.navigator = Navigator(
            self.device,
            log_callback=self.log,
            stop_check_callback=lambda: self.tab.task_stop_flag,
        )

        self.completed_count = 0
        self.in_battle_flow = False

    def _sleep(self, lo_ms, hi_ms):
        time.sleep(random.randint(lo_ms, hi_ms) / 1000.0)

    # ==================================================================
    #  工具
    # ==================================================================

    def _stopped(self):
        return (not self.running) or self.tab.task_stop_flag

    # ==================================================================
    #  主循环
    # ==================================================================

    def run(self):
        self.running = True
        self.log('👻 百鬼夜行运行中...')

        while self.running and not self.tab.task_stop_flag:
            screen = self.take_screenshot()
            if screen is None:
                self._sleep(250, 350)
                continue

            # 1. 结算画面
            if self.exists('baigui', 'baiguijiesuan', screen=screen):
                self.handle_settlement()
                continue

            # 2. 砸豆子窗口
            if self.exists('baigui', 'zadou', screen=screen):
                self.in_battle_flow = True
                self.throw_loop()
                continue

            # 3. 三选一界面
            if self.exists('baigui', 'sanxuanyi', screen=screen):
                self.in_battle_flow = True
                self.select_and_start()
                continue

            # 4. 百鬼准备界面
            if self.exists('baigui', 'baiguizhunbei', screen=screen):
                self.in_battle_flow = False
                self.enter_from_ready()
                continue

            # 5. 町中
            if self.navigator.is_interface_visible(screen, 'dingzhong'):
                self.in_battle_flow = False
                self.log('👉 点击百鬼入口')

                if not self.click_and_wait_switch(
                        click_fn=lambda: self.click_coord(
                            'baigui', 'baiguirukou'),
                        origin_check_fn=lambda s: self.navigator.is_interface_visible(
                            s, 'dingzhong'),
                        target_plugin='baigui', target_name='baiguizhunbei',
                        max_wait=10, max_click=3,
                        tag='百鬼入口'):
                    self.log('❌ 进入百鬼准备界面超时')
                    self.recover_to_dingzhong()
                continue

            # 6. 未知界面
            if self.in_battle_flow:
                self._sleep(800, 1200)
            else:
                self.recover_to_dingzhong()

            self._sleep(800, 1200)

        self.running = False

    # ==================================================================
    #  界面切换
    # ==================================================================

    def recover_to_dingzhong(self):
        while self.running and not self.tab.task_stop_flag:
            if self.navigator.go_to_dingzhong():
                return True
            self._sleep(800, 1200)
        return False

    def enter_from_ready(self):
        """准备界面 → 点 baiguijinru → 轮询等切到三选一"""
        if not self.click_and_wait_switch(
                click_fn=lambda: self.match_and_click('baigui', 'baiguijinru'),
                origin_check_fn=lambda s: self.tm.exists(
                    s, 'baigui', 'baiguizhunbei'),
                target_plugin='baigui', target_name='sanxuanyi',
                max_wait=10, max_click=3,
                tag='进入三选一'):
            self.log('❌ 进入三选一超时')
            self.recover_to_dingzhong()
            return False
        return True

    def select_and_start(self):
        """三选一界面随机选择 → 点 sanxuanyikaishi → 轮询等切到砸豆"""

        def click_and_start():
            name = random.choice(self.SANXUANYI_NAMES)
            self.click_coord('baigui', name)
            self._sleep(250, 350)
            return self.match_and_click('baigui', 'sanxuanyikaishi')

        if not self.click_and_wait_switch(
                click_fn=click_and_start,
                origin_check_fn=lambda s: self.tm.exists(
                    s, 'baigui', 'sanxuanyi'),
                target_plugin='baigui', target_name='zadou',
                max_wait=12, max_click=3,
                tag='进入砸豆'):
            self.log('❌ 进入砸豆超时')
            return

    # ==================================================================
    #  砸豆子
    # ==================================================================

    def throw_loop(self):
        """
        砸豆子循环：
        1. 延迟 2~3 秒
        2. 阶段 1（3 秒）：右 1/3
        3. 阶段 2（3 秒）：右 2/3
        4. 阶段 3：全区域
        全程优先点 buff
        """
        self.log('⏯️ 开始投豆子')

        self._sleep(int(self.THROW_START_DELAY[0] * 1000),
                    int(self.THROW_START_DELAY[1] * 1000))

        phase_start = time.time()

        while self.running and not self.tab.task_stop_flag:
            screen = self.take_screenshot()
            if screen is None:
                self._sleep(250, 350)
                continue

            if not self.exists('baigui', 'zadou', screen=screen):
                self.log('🏁 投豆子结束')
                return

            # 优先点 buff
            buff_clicked = False
            for buff_name in ('douzihuoqu', 'gailvup', 'jiasusadou'):
                if self.match_and_click('baigui', buff_name, screen=screen):
                    self.log(f'👉 点击buff')
                    buff_clicked = True
                    break

            if buff_clicked:
                self._sleep(300, 500)
                continue

            # 按阶段选区域
            elapsed = time.time() - phase_start
            if elapsed < self.THROW_PHASE_DURATION:
                self.click_coord('baigui', 'zadouyou1')
            elif elapsed < self.THROW_PHASE_DURATION * 2:
                self.click_coord('baigui', 'zadouyou2')
            else:
                self.click_coord('baigui', 'zadoufanwei')

            self._sleep(600, 1000)

    # ==================================================================
    #  结算
    # ==================================================================

    def handle_settlement(self):
        """结算画面 → 点 baiguijiesuan → 轮询等切到准备 → 计数"""
        self.log('⏯️ 结算')

        # 先检查达标，避免白打一轮
        if self.completed_count >= self.tab.max_count:
            self.log('🎉 次数达标')
            self.running = False
            return

        if not self.click_and_wait_switch(
                click_fn=lambda: self.match_and_click(
                    'baigui', 'baiguijiesuan'),
                origin_check_fn=lambda s: self.tm.exists(
                    s, 'baigui', 'baiguijiesuan'),
                target_plugin='baigui', target_name='baiguizhunbei',
                max_wait=15, max_click=3,
                tag='结算'):
            self.log('❌ 结算后界面切换超时')
            self.recover_to_dingzhong()
            return

        # 看到准备界面 → 确实完成了一局
        self.completed_count += 1
        self.log(f'🎯 完成 {self.completed_count} 次')
        self.log(f'📢 统计:百鬼:{self.completed_count}')

        if self.completed_count >= self.tab.max_count:
            self.log('🎉 次数达标')
            self.running = False
            return