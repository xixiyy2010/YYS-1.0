# ui/title_bar.py
"""
TitleBar —— 自定义标题栏

职责：
    - 显示标题文字
    - 深色模式开关
    - 无人值守开关（空壳）
    - 最小化 / 关闭按钮
    - 拖拽移动窗口
    - 主题切换时更新自身样式

注意：
    清除日志按钮已移除，挪到 Tab 内部的新增行。

依赖：ui.widgets.switch_button / PyQt6
"""
import ctypes

from PyQt6.QtWidgets import (
    QWidget, QHBoxLayout, QLabel, QPushButton,
)
from PyQt6.QtCore import Qt, pyqtSignal

from ui.widgets.switch_button import SwitchButton
from ui.widgets.hover_button import HoverButton

def _set_window_topmost(hwnd: int, enable: bool) -> bool:
    """
    用 Win32 SetWindowPos 直接改 TOPMOST 属性，不重建窗口。

    为什么不用 Qt 的 setWindowFlag：
        那个 API 会销毁原生 HWND 重建，重建设置期间窗口会闪一下。
        SetWindowPos 只改一个属性位，不重建，无闪烁。

    返回 True 表示成功；False 表示失败（调用方可以退回到 Qt 老方法）。
    """
    HWND_TOPMOST = -1
    HWND_NOTOPMOST = -2
    SWP_NOSIZE = 0x0001
    SWP_NOMOVE = 0x0002
    SWP_NOACTIVATE = 0x0010
    try:
        user32 = ctypes.windll.user32
        ok = user32.SetWindowPos(
            ctypes.c_void_p(hwnd),
            ctypes.c_void_p(HWND_TOPMOST if enable else HWND_NOTOPMOST),
            0, 0, 0, 0,
            SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE,
        )
        return bool(ok)
    except Exception:
        return False

class TitleBar(QWidget):
    minimize_clicked = pyqtSignal()
    close_clicked = pyqtSignal()
    theme_changed = pyqtSignal(bool)
    unattended_changed = pyqtSignal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(36)
        self.setCursor(Qt.CursorShape.ArrowCursor)

        self.is_dark = False
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 0, 8, 0)
        layout.setSpacing(6)

        self.title_label = QLabel("痒痒鼠")
        self.title_label.setStyleSheet(
            "color: #7A6F63; font-size: 9pt; background: transparent;")
        layout.addWidget(self.title_label)

        self.btn_top = QPushButton("📌")
        self.btn_top.setFixedSize(22, 22)
        self.btn_top.setCheckable(True)
        self.btn_top.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_top.clicked.connect(self._on_top_clicked)
        layout.addWidget(self.btn_top)

        layout.addStretch(1)

        self.switch_theme = SwitchButton()
        self.switch_theme.set_tip("深色模式", "below")
        self.switch_theme.state_changed.connect(self.theme_changed.emit)
        layout.addWidget(self.switch_theme)

        self.switch_unattended = SwitchButton()
        self.switch_unattended.set_tip("无人值守", "below")
        self.switch_unattended.state_changed.connect(self.unattended_changed.emit)
        layout.addWidget(self.switch_unattended)

        self.btn_min = HoverButton("—")
        self.btn_min.setFixedSize(33, 22)
        self.btn_min.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_min.clicked.connect(self.minimize_clicked.emit)
        layout.addWidget(self.btn_min)

        self.btn_close = HoverButton("X")
        self.btn_close.setFixedSize(33, 22)
        self.btn_close.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_close.clicked.connect(self.close_clicked.emit)
        layout.addWidget(self.btn_close)

        self._apply_top_button_style()
        self._apply_min_close_style()

    # ==================================================================
    #  最小化/关闭样式
    # ==================================================================

    def _apply_min_close_style(self):
        if self.is_dark:
            bg = "#423C35"
            fg = "#D6CCC2"
        else:
            bg = "#E5DED3"
            fg = "#6B5F52"
        normal = (f"QPushButton{{background:{bg};border:none;border-radius:6px;"
                  f"color:{fg};font-size:10pt;padding:0px;}}")
        hover = ("QPushButton{background:#C99B8A;border:none;border-radius:6px;"
                 "color:#FFFFFF;font-size:10pt;padding:0px;}")
        self.btn_min.set_hover_styles(normal, hover)
        self.btn_close.set_hover_styles(normal, hover)

    # ==================================================================
    #  置顶按钮
    # ==================================================================

    def _on_top_clicked(self, checked):
        w = self.window()
        hwnd = int(w.winId())
        _set_window_topmost(hwnd, bool(checked))
        self._apply_top_button_style()

    def _apply_top_button_style(self):
        if getattr(self, 'is_dark', False):
            base = "#423C35"
            hover = "#4E463E"
            pressed = "#3A352F"
            color = "#D6CCC2"
        else:
            base = "#E5DED3"
            hover = "#D9D0C2"
            pressed = "#CDC3B3"
            color = "#6B5F52"
        checked_bg = "#C99B8A"
        checked_color = "#FFFFFF"
        self.btn_top.setStyleSheet(f"""
            QPushButton {{
                background: {base};
                border: none;
                border-radius: 6px;
                color: {color};
                font-size: 9pt;
                padding: 0px;
            }}
            QPushButton:hover {{
                background: {hover};
            }}
            QPushButton:pressed {{
                background: {pressed};
            }}
            QPushButton:checked {{
                background: {checked_bg};
                color: {checked_color};
            }}
        """)

    # ==================================================================
    #  拖拽移动窗口
    # ==================================================================

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = (
                event.globalPosition().toPoint()
                - self.window().frameGeometry().topLeft())
            event.accept()

    def mouseMoveEvent(self, event):
        if event.buttons() & Qt.MouseButton.LeftButton:
            if hasattr(self, '_drag_offset'):
                self.window().move(
                    event.globalPosition().toPoint() - self._drag_offset)
            event.accept()

    # ==================================================================
    #  主题应用
    # ==================================================================

    def apply_theme(self, is_dark):
        self.is_dark = is_dark
        self.switch_theme.set_dark_mode(is_dark)
        self.switch_unattended.set_dark_mode(is_dark)
        self._apply_top_button_style()

        if is_dark:
            self.title_label.setStyleSheet(
                "color: #D6CCC2; font-size: 9pt; background: transparent;")
        else:
            self.title_label.setStyleSheet(
                "color: #7A6F63; font-size: 9pt; background: transparent;")

        self._apply_min_close_style()
        self.update()