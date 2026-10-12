# plugins/yys/liaotupo/liaotupo_task.py
import time
import random

from plugins.yys._shared.task_base import BaseTask
from plugins.yys._shared.navigation_engine import Navigator


class LiaoTupoTaskThread(BaseTask):
    """
    阴阳寮突破插件（网格驱动）

    改动要点（v2）：
        - 主循环 2 处界面切换改用 click_and_wait_switch：
            探索 → 个人突破 / 个人突破 → 寮突破
        - wait_for_battle_end 改两段式：
            第 1 段：等 zhandoutuichu 消失（最长 180 秒，PVP 可能打满）
            第 2 段：等 liaotupo 出现（最长 60 秒，容忍结算慢）
            超时时能区分"卡在战斗"和"卡在结算"，日志清晰
        - try_attack 保留原语义：同一格子不重试（可能是被寮友打过）
    """

    # ---------- 超时 ----------
    TIMEOUT_WAIT_BATTLE = 8          # 点进攻后等进战斗的最长时间（秒）
    TIMEOUT_WAIT_BATTLE_END = 180    # 等战斗结束（PVP 有时打满 3 分钟）
    TIMEOUT_WAIT_RETURN = 60         # 战斗结束后，等回到寮突破界面

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
        self.log('⛩️ 阴阳寮突破运行中...')

        while self.running and not self.tab.task_stop_flag:
            screen = self.take_screenshot()
            if screen is None:
                self._sleep(250, 350)
                continue

            # 1. 探索界面 → 点结界突破入口
            if self.exists('common', 'tansuo', screen=screen):
                self.log('👉 点击结界突破入口')

                if not self.click_and_wait_switch(
                        click_fn=lambda: self.match_and_click(
                            'common', 'jiejietupo'),
                        origin_check_fn=lambda s: self.tm.exists(
                            s, 'common', 'tansuo'),
                        target_plugin='collaboration',
                        target_name='gerentupo',
                        max_wait=10, max_click=3,
                        tag='结界突破'):
                    self.log('❌ 进入个人突破超时')
                    self.recover_to_tansuo()
                continue

            # 2. 个人突破界面 → 切换到寮突破
            if self.exists('collaboration', 'gerentupo', screen=screen):
                self.log('🔄 切换寮突破')

                if not self.click_and_wait_switch(
                        click_fn=lambda: self.click_coord(
                            'liaotupo', 'qiehuan'),
                        origin_check_fn=lambda s: self.tm.exists(
                            s, 'collaboration', 'gerentupo'),
                        target_plugin='liaotupo',
                        target_name='liaotupo',
                        max_wait=10, max_click=3,
                        tag='寮突破'):
                    self.log('❌ 切换到寮突破超时')
                    self.recover_to_tansuo()
                continue

            # 3. 寮突破界面 → 主循环
            if self.exists('liaotupo', 'liaotupo', screen=screen):
                if not self.liaotupo_loop():
                    self.recover_to_tansuo()
                continue

            # 4. 未知界面
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
    #  寮突破主循环
    # ==================================================================

    def liaotupo_loop(self):
        """每局：从第 1 个格子开始找未挑战的，依次尝试"""
        while self.running and not self.tab.task_stop_flag:
            if self.completed_count >= self.tab.max_count:
                self.log('🎉 次数达标')
                self.running = False
                return True

            screen = self.take_screenshot()
            if screen is None:
                self._sleep(250, 350)
                continue

            # 冷却检测
            if self.exists('liaotupo', '0_6', screen=screen):
                wait_time = random.randint(60, 120)
                self.log(f'💤 冷却中，等待 {wait_time} 秒')
                time.sleep(wait_time)
                continue

            # 找未挑战的格子
            unchallenged = self.get_unchallenged_grids()
            if not unchallenged:
                self.log('🏁 没有未挑战的格子，结束')
                self.running = False
                return True

            # 依次尝试
            failed_count = 0
            for idx in unchallenged:
                if failed_count >= 8:
                    break
                if self._stopped():
                    return False

                result = self.try_attack(idx)
                if result == 'battle':
                    if not self.wait_for_battle_end():
                        return False

                    # 战斗结束回到寮突破界面后，随机等 1~3 秒再继续
                    self._sleep(300, 500)

                    self.completed_count += 1
                    self.log(f'🎯 完成 {self.completed_count} 次')
                    self.log(f'📢 统计:寮突破:{self.completed_count}')
                    break
                elif result == 'failed':
                    failed_count += 1
                    self.log(f'❌ 格子 {idx+1} 失败({failed_count}/8)')
                    continue
                else:
                    return False

            if failed_count >= 8:
                self.log('❌ 8 次都进不去战斗，交给导航')
                return False

        return True

    def get_unchallenged_grids(self):
        """扫描『格子』网格，返回未挑战的索引列表"""
        results = self.grid_scan('liaotupo', '格子')
        if results is None:
            self.log('❌ 网格配置缺失: liaotupo/格子')
            return []
        return [r['idx'] for r in results if r['matched']]

    def try_attack(self, idx):
        """
        尝试对第 idx 个格子发起进攻。
        返回：'battle' / 'failed' / 'error'

        注：不重试同一格子——失败可能是这个格子已被寮友打过，
        换下一个格子才对。战斗完一次后网格会刷新，那个格子会变已挑战沉底。

        轮询逻辑：
            点格子 → 点进攻
            轮询窗口：
                看到 zhandoutuichu       → 'battle'
                连续 GRACE 秒原地不动    → 'failed'（进不去战斗）
                其它（过渡中）           → 继续轮询
            总超时 TIMEOUT_WAIT_BATTLE 秒
        """
        GRACE = 1.5   # 点进攻后，原地持续多少秒算失败

        # 1. 点格子
        self.grid_click('liaotupo', '格子', idx)
        self._sleep(500, 800)   # 给 UI 反应时间（点格子 → 进攻按钮激活）

        # 2. 点进攻
        self.log(f'⚔️ 进攻 格子{idx+1}')
        self.grid_click('liaotupo', '进攻', idx)

        # 3. 轮询等待进战斗
        start = time.time()
        liaotupo_since = None
        while time.time() - start < self.TIMEOUT_WAIT_BATTLE:
            if self._stopped():
                return 'error'
            screen = self.take_screenshot()
            if screen is None:
                continue

            # 进了战斗
            if self.exists('collaboration', 'zhandoutuichu', screen=screen):
                return 'battle'

            # 还在寮突破界面
            if self.exists('liaotupo', 'liaotupo', screen=screen):
                if liaotupo_since is None:
                    liaotupo_since = time.time()
                elif time.time() - liaotupo_since > GRACE:
                    # 转场早该开始，还看到原地 → 没进战斗
                    self.grid_click('liaotupo', '格子', idx)
                    return 'failed'
            else:
                # 过渡中（既不是战斗也不是原地）→ 重置计时
                liaotupo_since = None

            self._sleep(150, 250)

        # 总超时 → failed
        self.grid_click('liaotupo', '格子', idx)
        return 'failed'

    # ==================================================================
    #  战斗结束（两段式）
    # ==================================================================

    def wait_for_battle_end(self):
        """
        等战斗结束，回到寮突破界面（两段式）。

        第 1 段：等 collaboration/zhandoutuichu 消失（= 战斗结束）
                 最长 TIMEOUT_WAIT_BATTLE_END 秒（PVP 有时打满 3 分钟）
        第 2 段：等 liaotupo/liaotupo 出现（= 回到寮突破界面）
                 最长 TIMEOUT_WAIT_RETURN 秒（容忍结算慢）

        分两段的好处：
            - 卡在战斗里 → 日志明确说"等战斗结束超时"
            - 战斗结束但卡在结算 → 日志明确说"寮突破界面未回来"
            - 不会因单向判定被中间的"结算界面 / 未知界面"骗过
        """
        # 第一段：等战斗退出按钮消失
        if not self._wait_battle_exit_button():
            return False

        # 第二段：等寮突破界面回来
        if not self.wait_for_entry('liaotupo', 'liaotupo',
                                    max_wait=self.TIMEOUT_WAIT_RETURN,
                                    poll_interval=0.3):
            self.log('❌ 战斗结束后寮突破界面未回来')
            return False

        return True

    def _wait_battle_exit_button(self, timeout=None):
        """
        等 collaboration/zhandoutuichu 消失（= 战斗结束）。

        进入本方法前 caller 已通过 try_attack 确认看到过 zhandoutuichu，
        所以直接等它消失即可。
        """
        if timeout is None:
            timeout = self.TIMEOUT_WAIT_BATTLE_END

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

        self.log(f'❌ 等战斗结束超时(超过 {timeout} 秒)')
        return False