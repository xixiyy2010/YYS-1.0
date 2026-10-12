# plugins/yys/yuling/yuling_task.py
import time
import random

from plugins.yys._shared.task_base import BaseTask
from plugins.yys._shared.navigation_engine import Navigator


class YulingTaskThread(BaseTask):
    """
    御灵白狐单人插件（坐标走 source_<base>.json 的 coords/yuling）

    改动要点（v2）：
        - 主循环第 3 段（探索 → 御灵入口）改用 click_and_wait_switch。
        - select_baihu_and_challenge 改用 click_and_wait_switch。
        - challenge_loop 挑战流程改两段式：
            第 1 段：准备界面消失（点击被吞 → 15 秒超时，不误判）
            中间：  战斗中至少 5 秒
            第 2 段：准备界面回来（60 秒超时）
            两个都过 → +1。
          修掉原代码"点击被吞也误判为完成一局"的 bug。
    """

    # ---------- 超时 ----------
    TIMEOUT_READY_GONE = 15          # 点挑战后等准备界面消失（进战斗了）
    TIMEOUT_READY_BACK = 60          # 战斗结束后等准备界面回来
    BATTLE_MIN_WAIT = 5000           # 战斗中最少等待（毫秒）

    def __init__(self, tab, log_callback=None, stats_callback=None, error_callback=None):
        super().__init__(tab, log_callback=log_callback,
                         stats_callback=stats_callback, error_callback=error_callback)

        self.navigator = Navigator(
            self.device,
            log_callback=self.log,
            stop_check_callback=lambda: self.tab.task_stop_flag,
        )

        self.completed_count = 0

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
        self.log('🦊 御灵白狐运行中...')

        while self.running and not self.tab.task_stop_flag:
            screen = self.take_screenshot()
            if screen is None:
                self._sleep(250, 350)
                continue

            # 1. 御灵选择界面
            if self.exists('yuling', 'yulingxuanze', screen=screen):
                if not self.select_baihu_and_challenge():
                    self.recover_to_tansuo()
                    continue

            # 2. 御灵 boss 准备界面
            elif self.exists('yuling', 'yuling', screen=screen):
                if not self.challenge_loop():
                    self.recover_to_tansuo()
                    continue

            # 3. 探索界面 → 点御灵入口
            elif self.exists('common', 'tansuo', screen=screen):
                self.log('👉 点击御灵入口')

                if not self.click_and_wait_switch(
                        click_fn=lambda: self.match_and_click(
                            'yuling', 'yulingrukou'),
                        origin_check_fn=lambda s: self.tm.exists(
                            s, 'common', 'tansuo'),
                        target_plugin='yuling', target_name='yulingxuanze',
                        max_wait=10, max_click=3,
                        tag='御灵入口'):
                    self.log('❌ 进入御灵失败')
                    self.recover_to_tansuo()
                continue

            # 4. 未知界面
            else:
                self.recover_to_tansuo()

            self._sleep(800, 1200)

        self.running = False

    # ==================================================================
    #  导航
    # ==================================================================

    def recover_to_tansuo(self):
        while self.running and not self.tab.task_stop_flag:
            if self.navigator.go_to_tansuo():
                return True
            self._sleep(800, 1200)
        return False

    # ==================================================================
    #  选择白狐
    # ==================================================================

    def select_baihu_and_challenge(self):
        """御灵选择界面 → 点白狐 → 轮询等切到 boss 准备界面"""
        return self.click_and_wait_switch(
            click_fn=lambda: self.click_coord('yuling', 'baihu'),
            origin_check_fn=lambda s: self.tm.exists(
                s, 'yuling', 'yulingxuanze'),
            target_plugin='yuling', target_name='yuling',
            max_wait=10, max_click=3,
            tag='选择白狐')

    # ==================================================================
    #  挑战循环
    # ==================================================================

    def challenge_loop(self):
        """在 boss 准备界面持续挑战，直到次数达标"""
        while self.running and not self.tab.task_stop_flag:
            if self.completed_count >= self.tab.max_count:
                self.log('🎉 次数达标')
                return True

            screen = self.take_screenshot()
            if screen is None:
                return False

            if self.exists('yuling', 'sanceng', screen=screen):
                # 已选中第3层 → 点挑战按钮
                if random.random() < 0.85:
                    self._sleep(300, 1000)
                else:
                    self._sleep(1500, 3000)

                if not self.click_coord('yuling', 'tiaozhan'):
                    self.log('❌ 点击挑战失败')
                    return False
                self.log(f'⚔️ 开始挑战 第{self.completed_count + 1}次')
            else:
                # 没选中第3层 → 点 sanceng 区域切换
                self.log('🔍 未选中第3层，切换')
                self.click_coord('yuling', 'sanceng')
                self._sleep(500, 800)
                continue

            # ---- 两段式验证 ----
            # 御灵战斗界面没有稳定锚点模板，只能靠"准备界面消失 → 准备界面回来"两段式。
            # 不能只看"准备界面回来"——点击被吞时画面从头到尾都是准备界面，
            # 会直接误判为完成一局（这是原代码的 bug）。

            # 第 1 段：准备界面消失（= 进战斗了）
            if not self.wait_for_entry_disappear(
                    'yuling', 'yuling',
                    max_wait=self.TIMEOUT_READY_GONE,
                    poll_interval=0.2):
                self.log('❌ 点挑战后准备界面未消失')
                return False

            # 战斗中：至少等 BATTLE_MIN_WAIT（保留原意图，避免战斗过快导致漏判）
            self._sleep(self.BATTLE_MIN_WAIT, self.BATTLE_MIN_WAIT)

            # 第 2 段：准备界面回来（= 打完了）
            if not self.wait_for_entry(
                    'yuling', 'yuling',
                    max_wait=self.TIMEOUT_READY_BACK,
                    poll_interval=0.5):
                self.log('❌ 等待准备界面超时')
                return False

            self.completed_count += 1
            self.log(f'🎯 完成 {self.completed_count} 次')
            self.log(f'📢 统计:御灵:{self.completed_count}')

        return True