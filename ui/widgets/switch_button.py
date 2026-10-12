# widgets/switch_button.py
from PyQt6.QtWidgets import QPushButton, QLabel, QApplication
from PyQt6.QtCore import Qt, pyqtSignal, QRectF, QPoint, QTimer
from PyQt6.QtGui import QPainter, QColor

class SwitchButton(QPushButton):
    state_changed = pyqtSignal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setChecked(False)
        self.setFixedSize(44, 24)
        self.is_dark = False
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet("QPushButton { border: none; background: transparent; }")
        self.toggled.connect(self._on_toggled)

        # 固定位置提示
        self._tip_text = ''
        self._tip_position = 'below'
        self._tip_label = None
        self._tip_visible = False
        self._tip_allow_disabled = False   # 禁用状态下是否也允许显示 tip

        # 定时轮询鼠标位置：任何方向进入都能稳定显示
        self._tip_timer = QTimer(self)
        self._tip_timer.setInterval(150)
        self._tip_timer.timeout.connect(self._check_tip_by_cursor)
        self._tip_timer.start()


    def _on_toggled(self, checked):
        self.update()
        self.state_changed.emit(checked)

    def set_dark_mode(self, enabled):
        self.is_dark = enabled
        self._apply_tip_style()
        self.update()

    def set_tip(self, text, position='below'):
        """设置固定位置提示。
        position='below' → 提示显示在开关下方
        position='above' → 提示显示在开关上方
        """
        self._tip_text = text or ''
        self._tip_position = position

    def set_tip_allow_disabled(self, allow: bool):
        """是否允许在禁用状态下也显示 tip。
        用于"无功能"占位座位：禁用但悬停要提示。"""
        self._tip_allow_disabled = bool(allow)

    def _ensure_tip_label(self):
        if self._tip_label is None:
            lbl = QLabel(None)
            lbl.setWindowFlags(
                Qt.WindowType.Tool |
                Qt.WindowType.FramelessWindowHint |
                Qt.WindowType.WindowStaysOnTopHint |
                Qt.WindowType.WindowTransparentForInput
            )
            lbl.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
            lbl.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._tip_label = lbl
            self._apply_tip_style()
        return self._tip_label

    def _apply_tip_style(self):
        if self._tip_label is None:
            return
        if self.is_dark:
            bg = "#2B2723"
            fg = "#D6CCC2"
            border = "#4A443D"
        else:
            bg = "#FDFBF7"
            fg = "#3A2F25"
            border = "#D9D0C2"
        self._tip_label.setStyleSheet(
            f"QLabel {{ background: {bg}; color: {fg}; "
            f"border: 1px solid {border}; "
            f"border-radius: 4px; padding: 2px 6px; font-size: 9pt; }}"
        )

    def _check_tip_by_cursor(self):
        """定时器回调：按鼠标当前位置显示 / 隐藏提示"""
        if not self._tip_text:
            if self._tip_visible:
                self._hide_tip()
            return
        if not self.isVisible():
            if self._tip_visible:
                self._hide_tip()
            return
        if not self.isEnabled() and not self._tip_allow_disabled:
            if self._tip_visible:
                self._hide_tip()
            return
        from PyQt6.QtGui import QCursor
        global_pos = QCursor.pos()
        local_pos = self.mapFromGlobal(global_pos)
        inside = self.rect().contains(local_pos)
        if inside and not self._tip_visible:
            self._show_tip()
            self._tip_visible = True
        elif not inside and self._tip_visible:
            self._hide_tip()
            self._tip_visible = False

    def hideEvent(self, event):
        self._hide_tip()
        self._tip_visible = False
        super().hideEvent(event)

    def _show_tip(self):
        if not self._tip_text:
            return
        lbl = self._ensure_tip_label()
        lbl.setText(self._tip_text)
        lbl.adjustSize()

        top_left = self.mapToGlobal(QPoint(0, 0))
        w = lbl.width()
        h = lbl.height()
        x = top_left.x() + (self.width() - w) // 2

        if self._tip_position == 'above':
            y = top_left.y() - h - 4
        else:
            y = top_left.y() + self.height() + 4

        screen = QApplication.primaryScreen()
        if screen:
            avail = screen.availableGeometry()
            if x < avail.left() + 2:
                x = avail.left() + 2
            if x + w > avail.right() - 2:
                x = avail.right() - w - 2
            if y < avail.top() + 2:
                y = top_left.y() + self.height() + 4
            if y + h > avail.bottom() - 2:
                y = top_left.y() - h - 4

        lbl.move(x, y)
        lbl.show()
        lbl.raise_()

    def _hide_tip(self):
        if self._tip_label is not None:
            self._tip_label.hide()
        self._tip_visible = False

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        rect = QRectF(1, 1, self.width() - 2, self.height() - 2)
        radius = rect.height() / 2

        # 背景颜色
        if self.isChecked():
            bg_color = QColor(94, 161, 110)          # 绿色
        else:
            if self.is_dark:
                bg_color = QColor(0x4A, 0x44, 0x3D)  # 深色灰
            else:
                bg_color = QColor(185, 176, 164)     # 浅色灰

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(bg_color)
        painter.drawRoundedRect(rect, radius, radius)

        # 滑块位置
        diameter = rect.height() - 4
        if self.isChecked():
            x = rect.right() - diameter - 2
        else:
            x = rect.left() + 2
        y = rect.top() + 2

        painter.setBrush(QColor(255, 255, 255))
        painter.drawEllipse(QRectF(x, y, diameter, diameter))