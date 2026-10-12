# ui/__init__.py
"""
UI Layer —— 纯显示层

只做：
    - 显示
    - 发送用户意图信号
    - 订阅状态信号更新界面

不 import 业务层（plugins.*），不直接操作设备。
设备管理通过 Host 层 DeviceManager 完成。

依赖：contracts.* / infra.* / capability.hwnd / ui.widgets
"""
from .main_window import MainWindow

__all__ = ['MainWindow']