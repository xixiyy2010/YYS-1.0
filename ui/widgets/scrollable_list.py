# widgets/scrollable_list.py
from PyQt6.QtWidgets import (
    QScrollArea, QWidget, QFrame, QButtonGroup,
    QMenu, QLineEdit,
)
from PyQt6.QtCore import (
    Qt, QPoint, QTimer, QPropertyAnimation, QEasingCurve,
)
from PyQt6.QtGui import QAction, QCursor
from .indicator_button import IndicatorButton


class ScrollableButtonList(QScrollArea):

    SPACING = 2
    AUTO_SCROLL_MARGIN = 30
    AUTO_SCROLL_SPEED = 10
    ANIM_DURATION = 180

    def __init__(self, parent=None):
        super().__init__(parent)

        self._device_tab = None
        self.on_rename_callback = None
        self.on_order_changed_callback = None
        self.on_button_clicked_callback = None
        self.on_button_double_clicked_callback = None

        self.buttons = []
        self.edit_widget = None
        self.edit_button = None

        self._drag_btn = None
        self._drag_offset = QPoint()
        self._last_mouse_global = QPoint()

        self.setWidgetResizable(True)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setFrameShape(QFrame.Shape.NoFrame)

        self.container = QWidget()
        self.container.setMinimumHeight(0)
        self.setWidget(self.container)

        self.button_group = QButtonGroup()
        self.button_group.setExclusive(True)

        self._auto_scroll_timer = QTimer(self)
        self._auto_scroll_timer.setInterval(16)
        self._auto_scroll_timer.timeout.connect(self._on_auto_scroll_tick)

        self.verticalScrollBar().valueChanged.connect(self._on_scroll_changed)

    # ==================================================================
    #  按钮管理
    # ==================================================================

    def clear_all_buttons(self):
        self._force_cancel_drag()
        for btn in self.buttons:
            anim = getattr(btn, '_pos_anim', None)
            if anim is not None:
                anim.stop()
            self.button_group.removeButton(btn)
            btn.setParent(None)
            btn.deleteLater()
        self.buttons.clear()
        self._relayout(animated=False)

    def remove_button(self, btn):
        if btn not in self.buttons:
            return
        anim = getattr(btn, '_pos_anim', None)
        if anim is not None:
            anim.stop()
        self.button_group.removeButton(btn)
        self.buttons.remove(btn)
        btn.setParent(None)
        btn.deleteLater()
        self._relayout(animated=False)

    def add_button(self, text):
        btn = IndicatorButton(text)
        btn.set_full_text(text)
        btn.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        btn.customContextMenuRequested.connect(
            lambda pos, b=btn: self.show_context_menu(b, pos))
        btn.long_press.connect(self.start_drag)
        btn.double_clicked.connect(self._on_button_double_clicked)
        btn.drag_moved.connect(self._on_drag_moved)
        btn.drag_released.connect(self._on_drag_released)
        btn.clicked.connect(lambda checked, b=btn: self.on_button_clicked(b))

        btn.setParent(self.container)
        btn.move(0, 0)
        btn.show()

        self.button_group.addButton(btn)
        self.buttons.append(btn)
        self._relayout(animated=False)
        return btn

    def _on_button_double_clicked(self, btn):
        if self.on_button_double_clicked_callback:
            self.on_button_double_clicked_callback(btn)

    def insert_button_at(self, index, btn):
        self.button_group.addButton(btn)
        btn.setParent(self.container)
        btn.show()
        self.buttons.insert(index, btn)
        self._relayout(animated=False)

    def on_button_clicked(self, btn):
        if self.on_button_clicked_callback:
            self.on_button_clicked_callback(btn)

    # ==================================================================
    #  布局核心
    # ==================================================================

    def _relayout(self, animated=True):
        y = 0
        for btn in self.buttons:
            target = QPoint(0, y)
            if btn is self._drag_btn:
                pass
            elif animated:
                self._animate_to(btn, target)
            else:
                anim = getattr(btn, '_pos_anim', None)
                if anim is not None:
                    anim.stop()
                btn.move(target)
            y += btn.height() + self.SPACING

        if y > 0:
            y -= self.SPACING

        min_h = max(y, self.viewport().height())
        if self.container.minimumHeight() != min_h:
            self.container.setMinimumHeight(min_h)

    def _animate_to(self, btn, target):
        anim = getattr(btn, '_pos_anim', None)
        if anim is None:
            anim = QPropertyAnimation(btn, b"pos", self)
            anim.setEasingCurve(QEasingCurve.Type.OutCubic)
            anim.setDuration(self.ANIM_DURATION)
            btn._pos_anim = anim

        if btn.pos() == target:
            anim.stop()
            return

        anim.stop()
        anim.setStartValue(btn.pos())
        anim.setEndValue(target)
        anim.start()

    # ==================================================================
    #  拖动排序
    # ==================================================================

    def start_drag(self, btn):
        if self._drag_btn is not None or btn not in self.buttons:
            return

        self._drag_btn = btn

        press_global = getattr(btn, '_press_global_pos', None)
        if press_global is None:
            press_global = QCursor.pos()
        press_container = self.container.mapFromGlobal(press_global)
        self._drag_offset = press_container - btn.pos()

        self._apply_drag_effect(btn)
        btn.raise_()
        btn._drag_mode = True

        self._last_mouse_global = QCursor.pos()
        self._auto_scroll_timer.start()
        self._update_drag_position(self._last_mouse_global)

    def _on_drag_moved(self, btn, global_pos):
        if btn is not self._drag_btn:
            return
        self._last_mouse_global = global_pos
        self._update_drag_position(global_pos)
        self._update_order_by_mouse(global_pos)

    def _on_drag_released(self, btn):
        if btn is not self._drag_btn:
            return

        self._auto_scroll_timer.stop()
        self._clear_drag_effect(btn)
        btn._drag_mode = False

        self._drag_btn = None
        self._relayout(animated=True)

        if self.on_order_changed_callback:
            self.on_order_changed_callback(
                [b._full_text() for b in self.buttons])

    def _force_cancel_drag(self):
        if self._drag_btn is None:
            return
        self._auto_scroll_timer.stop()
        self._clear_drag_effect(self._drag_btn)
        self._drag_btn._drag_mode = False
        self._drag_btn = None

    def _update_drag_position(self, global_pos):
        if self._drag_btn is None:
            return

        local = self.container.mapFromGlobal(global_pos)
        x = local.x() - self._drag_offset.x()
        y = local.y() - self._drag_offset.y()

        max_x = max(0, self.container.width() - self._drag_btn.width())
        x = max(0, min(max_x, x))

        bar = self.verticalScrollBar()
        vp_top = bar.value()
        vp_bottom = vp_top + self.viewport().height()
        min_y = vp_top
        max_y = vp_bottom - self._drag_btn.height()
        if max_y < min_y:
            max_y = min_y
        y = max(min_y, min(max_y, y))

        self._drag_btn.move(x, y)

    def _update_order_by_mouse(self, global_pos):
        if self._drag_btn is None:
            return

        local = self.container.mapFromGlobal(global_pos)
        mouse_y = local.y()

        others = [b for b in self.buttons if b is not self._drag_btn]
        insert_index = len(others)
        y = 0
        for i, other in enumerate(others):
            center = y + other.height() / 2.0
            if mouse_y < center:
                insert_index = i
                break
            y += other.height() + self.SPACING

        new_order = list(others)
        new_order.insert(insert_index, self._drag_btn)

        if new_order == self.buttons:
            return

        self.buttons = new_order
        self._relayout(animated=True)

    def _on_auto_scroll_tick(self):
        if self._drag_btn is None:
            return
        bar = self.verticalScrollBar()
        if bar.maximum() <= bar.minimum():
            return

        vp_pos = self.viewport().mapFromGlobal(self._last_mouse_global)
        vp_h = self.viewport().height()
        margin = self.AUTO_SCROLL_MARGIN

        delta = 0
        if vp_pos.y() < margin:
            ratio = 1.0 - max(0, vp_pos.y()) / margin
            delta = -int(self.AUTO_SCROLL_SPEED * ratio)
        elif vp_pos.y() > vp_h - margin:
            ratio = 1.0 - max(0, vp_h - vp_pos.y()) / margin
            delta = int(self.AUTO_SCROLL_SPEED * ratio)

        if delta == 0:
            return

        new_val = max(bar.minimum(),
                      min(bar.maximum(), bar.value() + delta))
        if new_val != bar.value():
            bar.setValue(new_val)

    def _on_scroll_changed(self, value):
        if self._drag_btn is None:
            return
        self._update_drag_position(self._last_mouse_global)

    # ==================================================================
    #  拖动视觉（★ 只开关高亮，无阴影）
    # ==================================================================

    def _apply_drag_effect(self, btn):
        btn.set_drag_highlight(True)

    def _clear_drag_effect(self, btn):
        btn.set_drag_highlight(False)

    # ==================================================================
    #  右键菜单
    # ==================================================================

    def show_context_menu(self, btn, pos):
        menu = QMenu(self)

        style = btn.styleSheet() or ""
        is_running = ("7DBB8D" in style) or ("D08A7A" in style)

        # 开始 / 停止 二选一：按当前状态只显示一个
        if is_running:
            stop_action = QAction("停止运行", self)
            stop_action.triggered.connect(lambda: self._handle_stop(btn))
            menu.addAction(stop_action)
        else:
            start_action = QAction("开始运行", self)
            start_action.triggered.connect(lambda: self._handle_start(btn))
            menu.addAction(start_action)

        rename_action = QAction("重命名", self)
        rename_action.triggered.connect(lambda: self.start_rename(btn))
        menu.addAction(rename_action)

        main_window = self.window()
        is_dark = getattr(main_window, 'is_dark_theme', False)
        from styles import get_menu_style
        menu.setStyleSheet(get_menu_style(is_dark))

        global_pos = btn.mapToGlobal(QPoint(btn.width() + 5, 0))
        menu.exec(global_pos)

    def _handle_start(self, btn):
        """开始运行：直接调 DeviceTab 的处理逻辑"""
        self._device_tab._on_btn_double_clicked(btn)
    
    def _handle_stop(self, btn):
        """停止运行：直接调 DeviceTab 的处理逻辑"""
        self._device_tab._on_btn_double_clicked(btn)

    # ==================================================================
    #  重命名
    # ==================================================================

    def start_rename(self, btn):
        self.cancel_rename()
        self.edit_button = btn
        self.edit_widget = QLineEdit(self)
        self.edit_widget.setText(btn._full_text())
        self.edit_widget.setGeometry(
            btn.x(), btn.y(), btn.width(), btn.height())
        self.edit_widget.setFont(btn.font())
        self.edit_widget.selectAll()
        self.edit_widget.setFocus()
        self.edit_widget.show()
        self.edit_widget.returnPressed.connect(self.finish_rename)
        self.edit_widget.editingFinished.connect(self.finish_rename)
        self.edit_widget.installEventFilter(self)

        main_window = self.window()
        is_dark = getattr(main_window, 'is_dark_theme', False)
        from styles import get_line_edit_style
        self.edit_widget.setStyleSheet(get_line_edit_style(is_dark))

    def finish_rename(self):
        if not self.edit_button or not self.edit_widget:
            return
        if not self.edit_widget.isVisible():
            return
        new_text = self.edit_widget.text().strip()
        if new_text:
            self.edit_button.set_full_text(new_text)
            if self.on_rename_callback:
                self.on_rename_callback(self.edit_button, new_text)
        self.cancel_rename()

    def cancel_rename(self):
        if self.edit_widget:
            self.edit_widget.deleteLater()
            self.edit_widget = None
            self.edit_button = None

    def eventFilter(self, obj, event):
        if obj == self.edit_widget and event.type() == event.Type.KeyPress:
            if event.key() == 16777216:
                self.cancel_rename()
                return True
        return super().eventFilter(obj, event)

    # ==================================================================
    #  兼容接口
    # ==================================================================

    def set_order(self, order):
        if not order:
            return
        mapping = {btn._full_text(): btn for btn in self.buttons}
        new_buttons = []
        for text in order:
            if text in mapping:
                new_buttons.append(mapping[text])
        for btn in self.buttons:
            if btn not in new_buttons:
                new_buttons.append(btn)
        self.buttons = new_buttons
        self._relayout(animated=False)

    def get_order(self):
        return [btn._full_text() for btn in self.buttons]

    def wheelEvent(self, event):
        delta = event.angleDelta().y()
        if delta == 0:
            event.ignore()
            return
        bar = self.verticalScrollBar()
        if self.buttons:
            step = self.buttons[0].height() + self.SPACING
        else:
            step = 20
        if delta > 0:
            bar.setValue(bar.value() - step)
        else:
            bar.setValue(bar.value() + step)
        event.accept()