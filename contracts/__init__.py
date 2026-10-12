# contracts/__init__.py
"""
契约层：所有跨层通信的接口、事件、DTO 都定义在这里。

依赖规则（编译期）：
    contracts.*    → 不依赖任何东西（最底层）
    其它所有层     → 可依赖 contracts.*

契约层拆分：
    core        — 跨层基础设施接口（IEventBus / IPluginTask）
                  事件基类、通用事件名、通用 DTO、版本常量
    host        — Host 侧事件名（调度、配置、UI 意图、座位信号）
    worker      — Worker 侧事件名（回报、握手、崩溃报告）
    boss        — 业务老板接口（IBoss / BossReporter / BossState / TaskSpec / ToolKit）
    ui_protocol — UI ↔ 引擎通信协议（命令名 / 事件名 / 座位名 / 数据格式）

公开方式：
    推荐 from contracts import core, host, worker, boss, ui_protocol
    具体 DTO 再 from contracts.boss import IBoss, ToolKit 这样拿。
"""
from . import core, host, worker, boss, ui_protocol

__all__ = ['core', 'host', 'worker', 'boss', 'ui_protocol']