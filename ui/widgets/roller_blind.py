# ui/widgets/roller_blind.py
"""
RollerBar —— 卷帘画轴

只在卷起动画期间存在。展开态下这个控件根本不被创建。

依赖：PyQt6
"""
from PyQt6.QtWidgets import QWidget
from PyQt6.QtCore import Qt, QRectF
from PyQt6.QtGui import QPainter, QColor


class RollerBar(QWidget):
    """卷帘画轴：横向木杆 + 两端凸出把手 + 中间卷起的纸。"""

    HEIGHT = 16

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(self.HEIGHT)
        self.is_dark = False
        self._paper = 0.0   # 0=初始细纸卷(6px)  1=完全卷起(16px)

    def set_paper_thickness(self, v: float):
        v = max(0.0, min(1.0, float(v)))
        if abs(v - self._paper) < 1e-4:
            return
        self._paper = v
        self.update()

    def apply_theme(self, is_dark: bool):
        self.is_dark = bool(is_dark)
        self.update()

    def paintEvent(self, event):
        if self.width() <= 0 or self.height() <= 0:
            return

        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)

        w = self.width()
        h = self.height()

        handle_w = 14
        handle_thickness = 6

        if self.is_dark:
            handle_c = QColor("#3A2E22")
            cap_c    = QColor("#6A5A44")
            paper_c  = QColor("#5A4A38")
        else:
            handle_c = QColor("#8B6F47")
            cap_c    = QColor("#E8D5B0")
            paper_c  = QColor("#D4C4A0")

        # 中间纸卷：6px → 16px
        paper_h = handle_thickness + int(
            (h - handle_thickness) * self._paper)
        paper_y = (h - paper_h) / 2.0
        p.setBrush(paper_c)
        p.drawRoundedRect(
            QRectF(handle_w - 2, paper_y,
                   w - (handle_w - 2) * 2, paper_h),
            paper_h / 2.0, paper_h / 2.0)

        # 两端把手
        handle_y = (h - handle_thickness) / 2.0
        p.setBrush(handle_c)
        p.drawRoundedRect(
            QRectF(0, handle_y, handle_w + 4, handle_thickness), 3, 3)
        p.drawRoundedRect(
            QRectF(w - handle_w - 4, handle_y,
                   handle_w + 4, handle_thickness), 3, 3)

        # 端头圆帽
        p.setBrush(cap_c)
        p.drawEllipse(
            QRectF(0.5, handle_y - 0.5, 5, handle_thickness + 1))
        p.drawEllipse(
            QRectF(w - 5.5, handle_y - 0.5, 5, handle_thickness + 1))