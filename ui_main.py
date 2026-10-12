# ui_main.py
"""
UI 进程入口。

由 main.py 通过 subprocess.Popen 启动：
    python ui_main.py

职责：
    1. 起 QApplication
    2. 创建 EngineClient，尝试连接引擎（失败也继续，显示"引擎失联"）
    3. 创建 MainWindow（只依赖 engine_client + settings）
    4. 显示窗口，进入事件循环

依赖：
    contracts.ui_protocol
    ui.engine_client
    ui.main_window
    settings
    PyQt6
"""
import ctypes
import os
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from PyQt6.QtWidgets import QApplication

from ui.engine_client import EngineClient
from ui.main_window import MainWindow
from settings import Settings


def main():
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    # 配置（UI 侧只关心 window_pos 之类）
    settings = Settings(os.path.join(BASE_DIR, 'settings.json'))

    # 引擎客户端
    engine_client = EngineClient()

    # 窗口（先建出来，连不上引擎也能显示"引擎失联"）
    window = MainWindow(
        engine_client=engine_client,
        settings=settings,
    )
    window.show()

    # 尝试连接引擎（失败由 MainWindow 内部决定怎么显示）
    engine_client.connect_to_engine(timeout=3.0)

    sys.exit(app.exec())


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        import traceback
        print(f'❌ UI 启动失败: {e}', file=sys.stderr)
        traceback.print_exc()
        sys.exit(1)