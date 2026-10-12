# contracts/boss.py
"""
Contracts.Boss —— 业务老板接口（契约层）

业务侧：每个业务有一个 Boss（老板），它是业务的监督者。
引擎侧：引擎只认这个接口，通过它跟业务对话。

老板的职责：
    - 启停任务（start_task / stop_task）
    - 暂停 / 恢复（pause / resume）
    - 关闭（shutdown）
    - 接收座位操作（on_slot_command）—— UI 上用户操作座位时，引擎转发给老板
    - 上报（通过 reporter）：日志、状态、座位信号

中立原则：
    引擎不认识具体业务。老板上报的座位信号里，
    slot_name 是座位名（"alert" / "switch_1" / ...），
    引擎原样搬运，不解析含义。

ToolKit 语义（v2 收敛）：
    ToolKit 只承载"元数据 + 上下文"。
    真正的设备 IO 能力（截图 / 点击 / 滑动 / 模板匹配）
    通过 WorkerLauncher 下发给 Worker 子进程，
    老板不直接持有能力对象。

依赖：contracts.core（如需）
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any, List


# ==================================================================
#  老板状态
# ==================================================================

class BossState:
    """
    老板状态。

    idle     空闲（没跑任务）
    running  运行中（UI 标签页变绿）
    hanging  挂起
    error    出错（UI 标签页变红）
    stopping 停止中
    """
    IDLE = 'idle'
    RUNNING = 'running'
    HANGING = 'hanging'
    ERROR = 'error'
    STOPPING = 'stopping'


# ==================================================================
#  老板接口
# ==================================================================

class IBoss(abc.ABC):
    """
    业务老板接口。

    引擎只通过这个接口跟业务对话。
    """

    @property
    @abc.abstractmethod
    def name(self) -> str:
        """业务名（比如"阴阳师"）"""
        ...

    @abc.abstractmethod
    def start_task(self, task_id: str) -> bool:
        """启动任务。返回 True 表示已下发。"""
        ...

    @abc.abstractmethod
    def stop_task(self, task_id: str = '') -> None:
        """停止任务。task_id 为空表示停当前任务。"""
        ...

    @abc.abstractmethod
    def pause(self) -> None:
        ...

    @abc.abstractmethod
    def resume(self) -> None:
        ...

    @abc.abstractmethod
    def shutdown(self) -> None:
        """老板关闭。"""
        ...

    # ---- 座位操作（引擎 → 老板） ----

    def on_slot_command(self, slot_name: str, value) -> None:
        """
        UI 上用户操作了某个座位，引擎转发到这里。

        slot_name  座位名（"business" / "bind" / "alert" / "switch_1" / ...）
        value      操作后的值（开关位是 True/False，其他座位自己解释）

        默认空实现。业务按需覆盖。
        """
        pass


# ==================================================================
#  老板上报接口
# ==================================================================

class BossReporter(abc.ABC):
    """
    老板用来上报的接口（引擎实现，注入给老板）。

    老板通过它：
        - 写日志
        - 上报状态（UI 用它变标签页颜色）
        - 上报座位信号（UI 用它更新座位显示）
    """

    @abc.abstractmethod
    def state(self, state: str, detail: str = '') -> None:
        """上报老板状态（BossState 里的值）"""
        ...

    @abc.abstractmethod
    def log(self, message: str, level: str = 'info',
            ui_message: str = None,
            quiet: bool = False) -> None:
        """
        写日志。

        quiet=True  → 只落盘，不推 UI（主 UI / 悬浮窗都不播）。

        message     完整消息，落盘用。
        ui_message  UI 展示用，None 时等于 message。

        用途：
            完整消息含技术细节（如进程退出码），落盘留档。
            UI 只需用户看得懂的部分，通过 ui_message 精简。

        举例：
            self._reporter.log(
                '❌任务被中断 百鬼夜行: 进程退出 code=1',
                'error',
                ui_message='❌任务被中断 百鬼夜行: 进程退出',
            )
        """
        ...

    @abc.abstractmethod
    def slot(self, slot_name: str, value) -> None:
        """
        上报座位信号。

        slot_name  座位名（"alert" / "switch_1" / ...）
        value      值（提醒位："blink" / "off"；开关位：True / False）

        举例：
            self._reporter.slot('alert', 'blink')       # 让提醒位闪
            self._reporter.slot('switch_1', True)       # 开关1打开
        """
        ...


# ==================================================================
#  名片解析出的任务条目
# ==================================================================

@dataclass
class TaskSpec:
    """一张名片里声明的任务（对应 config.json tasks[] 的一项）"""
    task_id: str = ''
    name: str = ''
    class_name: str = ''
    module_file: str = ''
    folder: str = ''
    stats: List[str] = field(default_factory=list)
    default_count: str = 'inf'


# ==================================================================
#  引擎交给老板的工具包
# ==================================================================

@dataclass
class ToolKit:
    """
    引擎交给老板的工具包（元数据 + 上下文）。

    v2 收敛：
        移除了 screenshot / tap / swipe / color / template / shared_memory
        这 6 个字段。它们的语义模糊（曾被赋值为 bool），
        且业务层实际不通过它们调用设备 IO。

        设备 IO 能力由 WorkerLauncher 在启动 Worker 子进程时下发，
        子进程内通过 capability.* 直接使用。

    字段说明：
        base_dir        项目根目录绝对路径
        game_folder     名片目录名（引擎侧叫 app_id）
        templates_dir   业务模板目录绝对路径
        device_context  设备运行时上下文：
                            click_hwnd / shot_hwnd / shm_name
                            window_w / window_h / templates_dir
    """
    base_dir: str = ''
    game_folder: str = ''
    templates_dir: str = ''
    device_context: dict = field(default_factory=dict)