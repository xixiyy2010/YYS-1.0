# ui/custom_tab_bar.py
"""
CustomTabBar —— 自定义 QTabBar

职责：
    - 自绘 Tab（圆角背景 + 状态颜色 + 设备连接指示点 + 文字）
    - 加号按钮（发出 plus_clicked）
    - 双击 Tab（发出 tab_double_clicked）
    - 右键菜单：向左移动 / 向右移动 / 重命名 / 删除
    - Tab 重命名内联编辑

依赖：styles / PyQt6
"""
from PyQt6.QtWidgets import (
    QTabBar, QMenu, QLineEdit,
)
from PyQt6.QtCore import Qt, pyqtSignal, QRectF, QPoint, QPointF
from PyQt6.QtGui import QPainter, QColor, QAction


class CustomTabBar(QTabBar):
    plus_clicked = pyqtSignal()
    tab_double_clicked = pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.plus_rect = QRectF()
        self.edit_widget = None
        self.edit_index = -1
        self._plus_hovered = False
        self.setMouseTracking(True)

    # ==================================================================
    #  绘制
    # ==================================================================

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        tab_widget = self.parent()

        main_window = self.window()
        is_dark = getattr(main_window, 'is_dark_theme', False)

        dirty_rect = event.rect()

        for i in range(self.count()):
            widget = tab_widget.widget(i)
            if not widget:
                continue

            if i == self.edit_index:
                continue

            rect = self.tabRect(i).adjusted(1, 1, -1, -1)
            if not rect.intersects(dirty_rect):
                continue

            state = getattr(widget, 'tab_state', 'normal')
            selected = (i == self.currentIndex())

            if is_dark:
                bg_color = QColor(0x6A, 0x5F, 0x52) if selected else QColor(0x3B, 0x36, 0x30)
                text_color = QColor(0xFF, 0xFF, 0xFF) if selected else QColor(0xD6, 0xCC, 0xC2)
            else:
                bg_color = QColor(0xFD, 0xFB, 0xF7) if selected else QColor(0xE8, 0xE0, 0xD3)
                text_color = QColor(0x6A, 0x5F, 0x52)

            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(bg_color)
            painter.drawRoundedRect(rect, 8, 8)

            if state == 'green':
                painter.setBrush(QColor(91, 170, 107, 80))
                painter.drawRoundedRect(rect, 8, 8)
            elif state == 'red':
                painter.setBrush(QColor(201, 106, 90, 80))
                painter.drawRoundedRect(rect, 8, 8)
            elif state == 'flash':
                painter.setBrush(QColor(91, 170, 107, 80))
                painter.drawRoundedRect(rect, 8, 8)

            dot_radius = 4
            dot_x = rect.left() + 8
            dot_y = rect.top() + 8
            if getattr(widget, 'device_serial', None):
                dot_color = QColor(52, 199, 89)
            else:
                dot_color = QColor(142, 142, 147)
            painter.setBrush(dot_color)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawEllipse(QPointF(dot_x, dot_y), dot_radius, dot_radius)

            painter.setPen(text_color)
            font = painter.font()
            font.setPointSize(9)
            painter.setFont(font)
            text_rect = rect.adjusted(13, 0, -8, 0)
            if text_rect.width() <= 0:
                text_rect = rect.adjusted(10, 0, -8, 0)
            painter.drawText(text_rect,
                             Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                             self.tabText(i))

        # 加号按钮
        if self.count() > 0:
            last_rect = self.tabRect(self.count() - 1)
            x = last_rect.right() + 6
            y = (self.height() - 16) / 2
            plus_rect = QRectF(x, y, 16, 16)
            self.plus_rect = plus_rect
            if plus_rect.intersects(QRectF(dirty_rect)):
                painter.setPen(Qt.PenStyle.NoPen)
                if self._plus_hovered:
                    painter.setBrush(QColor(0xC9, 0x9B, 0x8A))
                else:
                    painter.setBrush(QColor(210, 200, 185))
                painter.drawRoundedRect(plus_rect, 4, 4)

                if self._plus_hovered:
                    painter.setPen(QColor(0xFF, 0xFF, 0xFF))
                else:
                    painter.setPen(QColor(80, 70, 60))
                font = painter.font()
                font.setPointSize(10)
                font.setBold(True)
                painter.setFont(font)
                painter.drawText(plus_rect, Qt.AlignmentFlag.AlignCenter, "+")

    # ==================================================================
    #  鼠标事件
    # ==================================================================

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            if self.plus_rect.isValid() and self.plus_rect.contains(event.position()):
                self.plus_clicked.emit()
                return
        elif event.button() == Qt.MouseButton.RightButton:
            pos = event.position().toPoint()
            index = self.tabAt(pos)
            if index >= 0:
                self.show_fixed_context_menu(index, pos)
                return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            index = self.tabAt(event.position().toPoint())
            if index >= 0:
                self.tab_double_clicked.emit(index)
                event.accept()
                return
        super().mouseDoubleClickEvent(event)

    def mouseMoveEvent(self, event):
        if self.plus_rect.isValid():
            hovered = self.plus_rect.contains(event.position())
            if hovered != self._plus_hovered:
                self._plus_hovered = hovered
                self.update()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):
        if self._plus_hovered:
            self._plus_hovered = False
            self.update()
        super().leaveEvent(event)

    # ==================================================================
    #  右键菜单
    # ==================================================================

    def show_fixed_context_menu(self, index, pos):
        menu = QMenu(self)
        tab_widget = self.parent()
        tab = tab_widget.widget(index)
        if tab is None:
            return

        count = self.count()

        # 向左移动：已在最左边就禁用
        move_left = QAction("向左移动", self)
        move_left.setEnabled(index > 0)
        move_left.triggered.connect(
            lambda checked=False, i=index: self._move_tab(i, i - 1))
        menu.addAction(move_left)

        # 向右移动：已在最右边就禁用
        move_right = QAction("向右移动", self)
        move_right.setEnabled(index < count - 1)
        move_right.triggered.connect(
            lambda checked=False, i=index: self._move_tab(i, i + 1))
        menu.addAction(move_right)

        menu.addSeparator()

        rename_action = QAction("重命名", self)
        rename_action.triggered.connect(
            lambda checked=False, i=index: self.start_rename_tab(i))
        menu.addAction(rename_action)

        # 删除标签：只有未绑定、且总数 > 1 时才显示
        is_bound = bool(getattr(tab, 'device_serial', None))
        if count > 1 and not is_bound:
            delete_action = QAction("删除标签", self)
            delete_action.triggered.connect(
                lambda checked=False, i=index: self.window().delete_tab(i))
            menu.addAction(delete_action)

        main_window = self.window()
        is_dark = getattr(main_window, 'is_dark_theme', False)
        from styles import get_menu_style
        menu.setStyleSheet(get_menu_style(is_dark))

        rect = self.tabRect(index)
        global_pos = self.mapToGlobal(QPoint(rect.left(), rect.bottom() + 2))
        menu.exec(global_pos)

    def _move_tab(self, from_index, to_index):
        """把标签从 from 移到 to（QTabBar 自带 moveTab）"""
        if from_index == to_index:
            return
        if from_index < 0 or from_index >= self.count():
            return
        if to_index < 0 or to_index >= self.count():
            return
        self.moveTab(from_index, to_index)

    # ==================================================================
    #  重命名
    # ==================================================================

    def start_rename_tab(self, index):
        self.cancel_rename()
        self.edit_index = index
        rect = self.tabRect(index)

        self.edit_widget = QLineEdit(self)
        self.edit_widget.setText(self.tabText(index))
        self.edit_widget.setGeometry(rect)
        self.edit_widget.setFont(self.font())
        self.edit_widget.selectAll()
        self.edit_widget.setFocus()
        self.edit_widget.show()
        self.edit_widget.installEventFilter(self)

        main_window = self.window()
        is_dark = getattr(main_window, 'is_dark_theme', False)
        from styles import get_line_edit_style
        self.edit_widget.setStyleSheet(get_line_edit_style(is_dark))

        self.edit_widget.textChanged.connect(self._on_rename_text_changed)
        self.edit_widget.editingFinished.connect(self._on_rename_finished)

    def _on_rename_text_changed(self, text):
        if self.edit_index < 0 or not self.edit_widget:
            return
        self.setTabText(self.edit_index, text if text else " ")
        rect = self.tabRect(self.edit_index)
        if rect.isValid():
            self.edit_widget.setGeometry(rect)

    def _on_rename_finished(self):
        if not self.edit_widget or self.edit_index < 0:
            return
        new_text = self.edit_widget.text().strip()
        if not new_text:
            new_text = self.tabText(self.edit_index)
        self.setTabText(self.edit_index, new_text)
        self.cancel_rename()

    def cancel_rename(self):
        if self.edit_widget:
            self.edit_widget.deleteLater()
            self.edit_widget = None
        self.edit_index = -1
        self.update()

    def eventFilter(self, obj, event):
        if obj == self.edit_widget and event.type() == event.Type.KeyPress:
            if event.key() == Qt.Key.Key_Escape:
                self.setTabText(self.edit_index, self.tabText(self.edit_index))
                self.cancel_rename()
                return True
        return super().eventFilter(obj, event)