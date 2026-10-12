# ui/widgets/hover_button.py
"""
HoverButton —— 鼠标悬停切换样式的按钮

跟 SwitchButton 用同一套轮询模式（QTimer 80ms 检查鼠标位置），
从任何方向、任何速度进入都能立即响应，比 QSS 的 :hover 稳定。

用法：
    btn = HoverButton("文字")
    btn.set_hover_styles(normal_style, hover_style)

主题切换时重新调 set_hover_styles 即可。
"""
from PyQt6.QtWidgets import QPushButton
from PyQt6.QtCore import QTimer
from PyQt6.QtGui import QCursor


class HoverButton(QPushButton):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._style_normal = ""
        self._style_hover = ""
        self._hovered = False

        self._hover_timer = QTimer(self)
        self._hover_timer.setInterval(150)
        self._hover_timer.timeout.connect(self._check_hover)
        self._hover_timer.start()

    def set_hover_styles(self, style_normal, style_hover):
        self._style_normal = style_normal or ""
        self._style_hover = style_hover or ""
        self._apply_current()

    def _apply_current(self):
        if self._hovered:
            if self._style_hover:
                self.setStyleSheet(self._style_hover)
        else:
            if self._style_normal:
                self.setStyleSheet(self._style_normal)

    def _check_hover(self):
        if not self.isVisible() or not self.isEnabled():
            if self._hovered:
                self._hovered = False
                self._apply_current()
            return
        inside = self.rect().contains(self.mapFromGlobal(QCursor.pos()))
        if inside != self._hovered:
            self._hovered = inside
            self._apply_current()
