# widgets/indicator_button.py
from PyQt6.QtWidgets import QPushButton
from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QPoint, QRectF
from PyQt6.QtGui import QPainter, QColor, QFontMetrics, QPen


class IndicatorButton(QPushButton):
    long_press = pyqtSignal(object)
    double_clicked = pyqtSignal(object)
    drag_moved = pyqtSignal(object, QPoint)
    drag_released = pyqtSignal(object)

    GREEN_STYLE = """
        QPushButton {
            border: 1px solid #5EA16E;
            border-radius: 10px;
            background: #7DBB8D;
            color: #FFFFFF;
            padding: 0px;
        }
        QPushButton:checked {
            border: 1px solid #5EA16E;
            border-radius: 10px;
            background: #7DBB8D;
            color: #FFFFFF;
            padding: 0px;
        }
        QPushButton:pressed {
            background: #6EAE7E;
        }
        QPushButton:hover {
            background: #8CC49B;
        }
    """
    RED_STYLE = """
        QPushButton {
            border: 1px solid #C97A6A;
            border-radius: 10px;
            background: #D08A7A;
            color: #FFFFFF;
            padding: 0px;
        }
        QPushButton:checked {
            border: 1px solid #C97A6A;
            border-radius: 10px;
            background: #D08A7A;
            color: #FFFFFF;
            padding: 0px;
        }
        QPushButton:pressed {
            background: #C17A6A;
        }
        QPushButton:hover {
            background: #D99B8A;
        }
    """

    def __init__(self, text, parent=None):
        super().__init__(text, parent)
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        font = self.font()
        size = font.pointSize()
        if size > 0:
            font.setPointSize(size + 1)
        else:
            font.setPointSize(10)
        self.setFont(font)

        self.setFixedHeight(self.calc_height())
        self.update_width()
        self.setStyleSheet("")
        self.setText("")

        self._press_timer = QTimer(self)
        self._press_timer.setSingleShot(True)
        self._press_timer.timeout.connect(self._on_long_press)
        self._long_pressed = False
        self._long_press_armed = False
        self._press_global_pos = None

        self._flash_timer = QTimer(self)
        self._flash_timer.setInterval(500)
        self._flash_timer.timeout.connect(self._toggle_flash_style)
        self._flash_enabled = False

        self.shake_offset_x = 0
        self._shake_timer = QTimer(self)
        self._shake_timer.setInterval(80)
        self._shake_timer.timeout.connect(self._on_shake_tick)
        self._shake_step = 0
        self._shake_max_steps = 0
        self._shake_directions = [-3, 3, -2, 2, -1, 1, 0]

        self._drag_mode = False
        self._drag_highlight = False

    def calc_height(self):
        fm = QFontMetrics(self.font())
        return fm.height() + 4

    def update_width(self):
        fm = QFontMetrics(self.font())
        text_width = fm.horizontalAdvance(self._full_text())
        width = 3 + 10 + 10 + text_width + 2
        self.setFixedWidth(width)

    def _full_text(self):
        return self.property("full_text") or ""

    def set_full_text(self, text):
        self.setProperty("full_text", text)
        self.update_width()

    # ==================================================================
    #  拖动高亮
    # ==================================================================

    def set_drag_highlight(self, enabled: bool):
        if self._drag_highlight != enabled:
            self._drag_highlight = enabled
            self.update()

    def _get_drag_highlight_color(self):
        is_dark = False
        try:
            w = self.window()
            if w is not None:
                is_dark = bool(getattr(w, 'is_dark_theme', False))
        except Exception:
            pass
        if is_dark:
            return QColor(232, 180, 160)   # #E8B4A0
        else:
            return QColor(201, 155, 138)   # #C99B8A

    def _on_shake_tick(self):
        if self._shake_step < self._shake_max_steps:
            self.shake_offset_x = self._shake_directions[
                self._shake_step % len(self._shake_directions)]
            self._shake_step += 1
            self.update()
        else:
            self._shake_timer.stop()
            self.shake_offset_x = 0
            self.update()

    def set_green_background(self, enabled):
        if enabled:
            self.setStyleSheet(self.GREEN_STYLE)
        else:
            self.setStyleSheet("")

    def set_red_background(self, enabled):
        if enabled:
            self.setStyleSheet(self.RED_STYLE)
        else:
            self.setStyleSheet("")
        self.style().unpolish(self)
        self.style().polish(self)
        self.update()

    def start_flash(self):
        self._flash_enabled = True
        self._flash_timer.start()

    def stop_flash(self):
        self._flash_enabled = False
        self._flash_timer.stop()
        self.setStyleSheet("")

    def _toggle_flash_style(self):
        if not self._flash_enabled:
            return
        if "7DBB8D" in self.styleSheet():
            self.setStyleSheet(self.RED_STYLE)
        else:
            self.setStyleSheet(self.GREEN_STYLE)

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        if self.shake_offset_x != 0:
            painter.translate(self.shake_offset_x, 0)

        # ★ 拖动高亮：只画边框，无叠加、无阴影
        if self._drag_highlight:
            color = self._get_drag_highlight_color()
            rect = QRectF(self.rect()).adjusted(1, 1, -1, -1)
            pen = QPen(color)
            pen.setWidth(2)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(rect, 10, 10)

        # 圆点（指示器）
        light_diameter = 10
        x = 3
        y = (self.height() - light_diameter) // 2
        color = QColor(94, 161, 110) if self.isChecked() else QColor(185, 176, 164)
        painter.setBrush(color)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(x, y, light_diameter, light_diameter)

        # 文字
        painter.setPen(self.palette().color(self.foregroundRole()))
        text_x = x + light_diameter + 6
        fm = QFontMetrics(self.font())
        text_y = (self.height() - fm.height()) // 2
        painter.drawText(text_x, text_y + fm.ascent(), self._full_text())

    def _on_long_press(self):
        if not self._long_press_armed:
            return
        self._long_pressed = True
        self.long_press.emit(self)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._press_global_pos = event.globalPosition().toPoint()
            self._long_press_armed = True
            self._press_timer.start(1000)
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag_mode:
            self.drag_moved.emit(self, event.globalPosition().toPoint())
            event.accept()
            return

        if self._long_press_armed:
            local = event.position().toPoint()
            if not self.rect().contains(local):
                self._long_press_armed = False
                self._press_timer.stop()

        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._press_timer.stop()
            self._long_press_armed = False
            if self._drag_mode:
                self.drag_released.emit(self)
                event.accept()
                return
            if self._long_pressed:
                self._long_pressed = False
                return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.double_clicked.emit(self)
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def reset_style(self):
        self.setStyleSheet("")
        self.style().unpolish(self)
        self.style().polish(self)
        self.update()

    def shake(self):
        self._shake_step = 0
        self._shake_max_steps = len(self._shake_directions) * 2
        self._shake_timer.start()


class MechanicalButton(QPushButton):
    GREEN_STYLE = """
        QPushButton {
            border: 1px solid #5EA16E;
            border-radius: 10px;
            background: #7DBB8D;
            color: #FFFFFF;
            padding: 2px 8px;
        }
        QPushButton:pressed {
            background: #6EAE7E;
        }
        QPushButton:hover {
            background: #8CC49B;
        }
    """

    def __init__(self, text, parent=None):
        super().__init__(text, parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        font = self.font()
        size = font.pointSize()
        if size > 0:
            font.setPointSize(size + 1)
        else:
            font.setPointSize(10)
        self.setFont(font)

        self.setFixedHeight(self.calc_height())
        self.update_width()
        self.setStyleSheet("")

    def calc_height(self):
        fm = QFontMetrics(self.font())
        return fm.height() + 8

    def update_width(self):
        fm = QFontMetrics(self.font())
        text_width = fm.horizontalAdvance(self.text())
        width = text_width + 22
        self.setFixedWidth(width)

    def set_green_background(self, enabled):
        if enabled:
            self.setStyleSheet(self.GREEN_STYLE)
        else:
            self.setStyleSheet("")