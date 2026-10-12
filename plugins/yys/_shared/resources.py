# plugins/yys/_shared/resources.py
"""
资源定义 + 状态定义（业务层）

"资源"是系统中需要被某方独占使用的能力。
"状态"是设备当前所处的情境（由业务层上报）。

加新资源：ResourceNames 里加常量 + STATE_PAUSE_MAP 里加一行。
加新状态：StateNames 里加常量 + STATE_PAUSE_MAP 里加一行。

依赖：无
"""


class ResourceNames:
    """资源名（字符串常量）。AutoScheduler 按这些名字仲裁。"""
    # "跳过结算"处理权：对应 JixuWorker 的 jixu 系列处理
    SKIP_SETTLEMENT = 'skip_settlement'


class StateNames:
    """状态名（字符串常量）。业务上报时使用。"""
    TUPO_8_2 = 'tupo_8_2'


# 状态名 → 该状态存在时，需要暂停的资源列表
#
# 当某个设备处于该状态时，列出的资源被暂停。
# 每台设备的 AutoScheduler 独立仲裁：该设备退出该状态后，
# 本设备对应的资源恢复（资源仲裁是"设备级"，不是全局）。
#
# 举例：
#   打8退2期间，禁止全局调度器抢点"继续/跳过结算"，
#   让业务代码自己走退出 → 再次挑战流程
STATE_PAUSE_MAP = {
    StateNames.TUPO_8_2: [ResourceNames.SKIP_SETTLEMENT],
}