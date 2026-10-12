# plugins/yys/_shared/__init__.py
"""
plugins.yys._shared —— 跨插件共享的业务组件

门面：业务插件直接 `from plugins.yys._shared import ...`。

包含：
    ResourceNames / StateNames / STATE_PAUSE_MAP   资源名、状态名、调度表
    BaseTask                                        任务基类
    Navigator                                       智能导航
    AutoScheduler                                   全局调度层（监督者）
    XuanshangWorker                                 悬赏协助处理器
    JixuWorker                                      跳过结算处理器

主架构（contracts / infra / host / worker / adapters / ui）不 import 本包。
本包只能被 plugins/* 的具体插件 import。

注意：AutoScheduler / XuanshangWorker / JixuWorker 是【类】，实例化由
boss.py 在老板构造时完成，本包只导出类定义。
"""
from .resources import (
    ResourceNames,
    StateNames,
    STATE_PAUSE_MAP,
)
from .task_base import BaseTask
from .navigation_engine import Navigator
from .auto_scheduler import AutoScheduler
from .xuanshang_worker import XuanshangWorker
from .jixu_worker import JixuWorker

__all__ = [
    'ResourceNames',
    'StateNames',
    'STATE_PAUSE_MAP',
    'BaseTask',
    'Navigator',
    'AutoScheduler',
    'XuanshangWorker',
    'JixuWorker',
]