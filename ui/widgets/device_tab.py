# ui/widgets/device_tab.py
"""
DeviceTab —— 单个设备 Tab（纯 UI 层）

座位机制：
    名片（slots 段）声明业务要坐哪些座位：
        business  → 绑定 self.app_combo（唯一）
        bind      → 绑定 self.btn_bind（唯一）
        alert     → 无控件、无文字，只用于触发标签页闪烁（唯一）
        switches  → 动态建一排开关（可多个）

    业务不声明 → 对应座位显示"无功能"。
    用户操作座位 → 发 slot_command 信号 → MainWindow → 引擎。
    引擎推 slot_changed → on_slot_changed() → 更新对应控件。

中立原则：
    本文件不认识"游戏 / 模拟器"。字段名中性。
    slot_name 是座位名（"alert" / "switch_1" / ...），
    本文件只按声明动态建控件，不解析含义。

依赖：
    ui.widgets.scrollable_list / indicator_button / switch_button
    PyQt6
"""
from types import SimpleNamespace
import time

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLineEdit, QComboBox,
    QPushButton, QPlainTextEdit, QLabel,
)
from PyQt6.QtCore import Qt, pyqtSignal, QPoint, QEvent, QTimer
from PyQt6.QtGui import QPixmap, QPainter, QColor, QPolygon

from ui.widgets.scrollable_list import ScrollableButtonList
from ui.widgets.indicator_button import MechanicalButton
from ui.widgets.switch_button import SwitchButton
from ui.widgets.hover_button import HoverButton
from styles import LIGHT_COLORS, DARK_COLORS


class DeviceTab(QWidget):
    log_message = pyqtSignal(str)
    app_changed = pyqtSignal(str)
    bind_clicked = pyqtSignal(object)
    slot_command = pyqtSignal(object, str, object)   # (tab, slot_name, value)
    clear_log_clicked = pyqtSignal(object)
    request_start_task = pyqtSignal(object, str)
    request_stop_task = pyqtSignal(object)
    request_list_apps = pyqtSignal(object)

    def __init__(self, index, settings, parent=None):
        super().__init__(parent)
        self.index = index
        self._settings = settings

        self.device_serial = None
        self.target_title = None
        self.window_hwnd = None
        self.adapter_key = None
        self.current_app = None
        self.tab_state = 'normal'
        self.max_count = float('inf')

        self.current_func_btn = None
        self.current_running_module = None

        # 运行时间
        self._run_start_time = None
        self._run_timer = QTimer(self)
        self._run_timer.setInterval(1000)
        self._run_timer.timeout.connect(self._update_run_time)

        # 引擎喂进来的方案列表
        self._plugins = []

        # 座位声明（名片里的 slots 段）
        self._slots = {}
        # 动态建的开关控件：slot_name -> SwitchButton
        self._switch_widgets = {}
        # 提醒位是否激活（只影响标签页颜色，不显示文字）
        self._alert_active = False

        self.info_lines = [
            "📢 重要信息:信息:0 信息:0",
            "📢 当前信息:信息:0 信息:0",
            "实时日志1",
            "实时日志2",
            "实时日志3",
            "实时日志4",
            "实时日志5"
        ]

        self.button_plugin_map = {}

        self._build_ui()
        self._apply_slots_ui()
        self._apply_bind_button_style()
        self._apply_app_combo_style()
        self._apply_clear_log_style()
        self._apply_refresh_style()
        self._apply_count_reset_style()
        self._apply_scrollbar_style()
        self.app_combo.installEventFilter(self)

    # ==================================================================
    #  UI 构建
    # ==================================================================

    def _build_ui(self):
        main_layout = QVBoxLayout()
        main_layout.setContentsMargins(5, 5, 8, 5)
        main_layout.setSpacing(5)

        # ---- 顶部工具行 ----
        self.tool_row = QHBoxLayout()
        self.tool_row.setContentsMargins(0, 0, 0, 0)
        self.tool_row.setSpacing(6)

        # 业务位（A）：下拉框
        self.app_combo = QComboBox()
        self.app_combo.setMinimumWidth(80)
        self.app_combo.setFixedHeight(24)
        self.app_combo.currentIndexChanged.connect(self._on_app_combo_changed)
        self.tool_row.addWidget(self.app_combo)

        # 绑定位（B）：按钮
        self.btn_bind = HoverButton("绑定")
        self.btn_bind.setFixedHeight(22)
        self.btn_bind.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_bind.clicked.connect(lambda: self.bind_clicked.emit(self))
        self.tool_row.addWidget(self.btn_bind)

        # 提醒位（C）：无控件、无文字。
        #   业务发 alert 信号 → 标签页闪（走 tab_state='flash'）。
        #   这里什么都不放。

        # 开关位：动态建（见 _apply_slots_ui）
        self._switch_container = QWidget()
        self._switch_layout = QHBoxLayout(self._switch_container)
        self._switch_layout.setContentsMargins(0, 0, 0, 0)
        self._switch_layout.setSpacing(6)
        self.tool_row.addStretch(1)
        self.tool_row.addWidget(self._switch_container)

        # 清除日志：UI 自带，固定
        self.btn_clear_log = HoverButton("清理日志")
        self.btn_clear_log.setFixedHeight(24)
        self.btn_clear_log.setFixedWidth(72)
        self.btn_clear_log.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_clear_log.clicked.connect(
            lambda: self.clear_log_clicked.emit(self))
        self.tool_row.addWidget(self.btn_clear_log)

        main_layout.addLayout(self.tool_row)

        # ---- 中部：按钮列表 + 日志 ----
        self.scroll_list = ScrollableButtonList()
        self.scroll_list._device_tab = self
        self.scroll_list.on_rename_callback = self._on_rename
        self.scroll_list.on_order_changed_callback = self._on_order_changed
        self.scroll_list.on_button_clicked_callback = self._on_btn_clicked
        self.scroll_list.on_button_double_clicked_callback = self._on_btn_double_clicked

        middle_layout = QHBoxLayout()
        middle_layout.setSpacing(5)

        left_layout = QVBoxLayout()
        left_layout.setSpacing(5)

        self.btn_refresh = HoverButton("刷新方案")
        from PyQt6.QtGui import QFontMetrics as _FM
        _fm = _FM(self.btn_refresh.font())
        self.btn_refresh.setFixedHeight(28)
        self.btn_refresh.setFixedWidth(_fm.horizontalAdvance("刷新方案") + 22)
        self.btn_refresh.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_refresh.clicked.connect(lambda: self._reload_plugins(silent=False))
        left_layout.addWidget(self.btn_refresh)

        count_row = QHBoxLayout()
        count_row.setContentsMargins(0, 0, 0, 0)

        self.btn_count_reset = HoverButton("次")
        self.btn_count_reset.setFixedWidth(22)
        self.btn_count_reset.setFixedHeight(24)
        self.btn_count_reset.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_count_reset.clicked.connect(self._reset_count_input)

        self.count_input = QLineEdit("inf")
        self.count_input.setFixedWidth(50)
        self.count_input.setFixedHeight(24)
        self.count_input.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        self.count_input.setAlignment(Qt.AlignmentFlag.AlignRight)

        count_row.addWidget(self.count_input)
        count_row.addWidget(self.btn_count_reset)
        count_row.addStretch(1)
        left_layout.addLayout(count_row)

        left_layout.addWidget(self.scroll_list, 1)
        middle_layout.addLayout(left_layout, 0)

        right_layout = QVBoxLayout()
        right_layout.setSpacing(3)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(100)
        right_layout.addWidget(self.log_view, 1)

        # 运行时间
        self.run_time_label = QLabel("就绪")
        self.run_time_label.setFixedHeight(20)
        self.run_time_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.run_time_label.setStyleSheet(
            f"QLabel {{ color: {LIGHT_COLORS['text']}; font-size: 9pt; "
            "background: transparent; padding-right: 4px; }}")
        right_layout.addWidget(self.run_time_label)

        middle_layout.addLayout(right_layout, 1)

        main_layout.addLayout(middle_layout, stretch=1)
        self.setLayout(main_layout)

    # ==================================================================
    #  座位（名片声明）
    # ==================================================================

    def set_slots(self, slots: dict):
        """接收名片里的座位声明（slots 段）"""
        self._slots = dict(slots or {})
        self._apply_slots_ui()

    def _apply_slots_ui(self):
        """根据 self._slots 重建 / 更新座位控件

        座位是 UI 固定的，业务只是决定坐不坐：
            - 业务声明了对应数量的开关 → 座位按声明显示 label + default + 可点
            - 业务没声明够 → 剩下的座位显示"无功能"占位（禁用 + 悬停提示）
            - 业务声明超过固定数量 → 超出部分忽略（座位固定，业务多余声明无效）
        """
        # ---- 提醒位（C）：无控件、无文字。业务声明与否都不显示。 ----
        # 提醒位的效果 = 标签页闪，走 on_slot_changed('alert', 'blink')

        # ---- 开关位：固定 2 个座位 ----
        while self._switch_layout.count():
            item = self._switch_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        self._switch_widgets.clear()

        FIXED_SWITCH_COUNT = 2
        declared = self._slots.get('switches') or []
        is_dark = getattr(self.window(), 'is_dark_theme', False)

        for i in range(FIXED_SWITCH_COUNT):
            slot_name = f'switch_{i + 1}'
            has_function = (i < len(declared))

            switch = SwitchButton()
            switch.set_dark_mode(is_dark)

            if has_function:
                sw_cfg = declared[i]
                label = sw_cfg.get('label', '')
                default = bool(sw_cfg.get('default', False))

                if self.device_serial:
                    switch.setChecked(default)
                    switch.setEnabled(True)
                else:
                    switch.setChecked(False)
                    switch.setEnabled(False)

                if label:
                    switch.set_tip(label, "above")
                    switch.set_tip_allow_disabled(True)
                switch.state_changed.connect(
                    lambda v, sn=slot_name: self._on_switch_toggled(sn, v))
            else:
                switch.setChecked(False)
                switch.setEnabled(False)
                switch.set_tip("无功能", "above")
                switch.set_tip_allow_disabled(True)

            self._switch_widgets[slot_name] = switch
            self._switch_layout.addWidget(switch)

    def _on_switch_toggled(self, slot_name: str, value: bool):
        """用户点了开关 → 发信号给 MainWindow → 引擎"""
        self.slot_command.emit(self, slot_name, bool(value))

    def _ensure_arrow_icon(self, color_hex):
        """生成一个小三角形 PNG 图标，返回文件路径。按颜色缓存。"""
        import os
        import tempfile

        icons_dir = os.path.join(tempfile.gettempdir(), 'yys_icons')
        os.makedirs(icons_dir, exist_ok=True)

        safe = color_hex.lstrip('#')
        path = os.path.join(icons_dir, f'arrow_{safe}.png')

        if not os.path.exists(path):
            pm = QPixmap(8, 6)
            pm.fill(Qt.GlobalColor.transparent)

            p = QPainter(pm)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            p.setBrush(QColor(color_hex))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawPolygon(QPolygon([
                QPoint(0, 0), QPoint(8, 0), QPoint(4, 6)
            ]))
            p.end()

            pm.save(path, 'PNG')

        return path.replace('\\', '/')

    def _apply_bind_button_style(self):
        is_dark = getattr(self.window(), 'is_dark_theme', False)
        is_bound = bool(self.device_serial)
        is_enabled = self.btn_bind.isEnabled()

        if is_bound:
            if is_enabled:
                normal = ("QPushButton{background:#5EA16E;color:#FFFFFF;"
                          "border:1px solid #4B8B5A;border-radius:10px;padding:0 6px;}")
                hover = ("QPushButton{background:#C99B8A;color:#FFFFFF;"
                         "border:1px solid #B88070;border-radius:10px;padding:0 6px;}")
            else:
                normal = ("QPushButton{background:#B8D4BE;color:#3A5A42;"
                          "border:1px solid #A3C7AB;border-radius:10px;padding:0 6px;}")
                hover = normal
        else:
            if is_dark:
                normal = ("QPushButton{background:#423C35;color:#D6CCC2;"
                          "border:1px solid #4A443D;border-radius:10px;padding:0 6px;}")
            else:
                normal = ("QPushButton{background:#E5DED3;color:#6B5F52;"
                          "border:1px solid #D9D0C2;border-radius:10px;padding:0 6px;}")
            hover = ("QPushButton{background:#C99B8A;color:#FFFFFF;"
                     "border:1px solid #B88070;border-radius:10px;padding:0 6px;}")
        self.btn_bind.set_hover_styles(normal, hover)

    def _apply_clear_log_style(self):
        is_dark = getattr(self.window(), 'is_dark_theme', False)
        if is_dark:
            bg = "#423C35"
            fg = "#D6CCC2"
            border = "1px solid #4A443D"
        else:
            bg = "#E5DED3"
            fg = "#6B5F52"
            border = "1px solid #D9D0C2"
        normal = (f"QPushButton{{background:{bg};color:{fg};border:{border};"
                  f"border-radius:6px;padding:0 6px;}}")
        hover = ("QPushButton{background:#C99B8A;color:#FFFFFF;"
                 "border:1px solid #B88070;border-radius:6px;padding:0 6px;}")
        self.btn_clear_log.set_hover_styles(normal, hover)

    def _apply_refresh_style(self):
        is_dark = getattr(self.window(), 'is_dark_theme', False)
        fg = "#D6CCC2" if is_dark else "#6B5F52"
        normal = (f"QPushButton{{background:transparent;border:none;color:{fg};"
                  f"border-radius:6px;padding:2px 8px;}}")
        hover = ("QPushButton{background:#C99B8A;color:#FFFFFF;border:none;"
                 "border-radius:6px;padding:2px 8px;}")
        self.btn_refresh.set_hover_styles(normal, hover)

    def _apply_scrollbar_style(self):
        is_dark = getattr(self.window(), 'is_dark_theme', False)
        if is_dark:
            handle = "rgba(255,255,255,60)"
            handle_hover = "rgba(255,255,255,130)"
        else:
            handle = "rgba(0,0,0,55)"
            handle_hover = "rgba(0,0,0,120)"
        qss = (
            "QScrollBar:vertical {"
            "  background: transparent;"
            "  width: 8px;"
            "  margin: 0px;"
            "  border: none;"
            "}"
            "QScrollBar::handle:vertical {"
            f"  background: {handle};"
            "  min-height: 24px;"
            "  border-radius: 4px;"
            "}"
            "QScrollBar::handle:vertical:hover {"
            f"  background: {handle_hover};"
            "}"
            "QScrollBar::add-line:vertical,"
            "QScrollBar::sub-line:vertical {"
            "  height: 0px;"
            "  background: transparent;"
            "  border: none;"
            "}"
            "QScrollBar::add-page:vertical,"
            "QScrollBar::sub-page:vertical {"
            "  background: transparent;"
            "}"
            "QScrollBar:horizontal {"
            "  background: transparent;"
            "  height: 8px;"
            "  margin: 0px;"
            "  border: none;"
            "}"
            "QScrollBar::handle:horizontal {"
            f"  background: {handle};"
            "  min-width: 24px;"
            "  border-radius: 4px;"
            "}"
            "QScrollBar::handle:horizontal:hover {"
            f"  background: {handle_hover};"
            "}"
            "QScrollBar::add-line:horizontal,"
            "QScrollBar::sub-line:horizontal {"
            "  width: 0px;"
            "  background: transparent;"
            "  border: none;"
            "}"
            "QScrollBar::add-page:horizontal,"
            "QScrollBar::sub-page:horizontal {"
            "  background: transparent;"
            "}"
        )
        self.log_view.setStyleSheet(qss)
        self.scroll_list.setStyleSheet(qss)

    def _apply_count_reset_style(self):
        is_dark = getattr(self.window(), 'is_dark_theme', False)
        if is_dark:
            bg = "#423C35"
            fg = "#D6CCC2"
            border = "1px solid #4A443D"
        else:
            bg = "#E5DED3"
            fg = "#6B5F52"
            border = "1px solid #D9D0C2"
        normal = (f"QPushButton{{background:{bg};color:{fg};border:{border};"
                  f"border-radius:6px;padding:0px;}}")
        hover = ("QPushButton{background:#C99B8A;color:#FFFFFF;"
                 "border:1px solid #B88070;border-radius:6px;padding:0px;}")
        self.btn_count_reset.set_hover_styles(normal, hover)

    def _apply_app_combo_style(self):
        is_dark = getattr(self.window(), 'is_dark_theme', False)
        is_bound = bool(self.device_serial)

        if is_dark:
            bg = '#332F2A'
            fg = '#D6CCC2'
            border = '#4A443D'
            arrow_color = '#D6CCC2'
        else:
            bg = '#FDFBF7'
            fg = '#6B5F52'
            border = '#D9D0C2'
            arrow_color = '#6B5F52'

        if is_bound:
            arrow_color = '#FFFFFF'

        arrow_path = self._ensure_arrow_icon(arrow_color)

        if is_bound:
            combo_base = (
                "QComboBox{background:#B8D4BE;color:#3A5A42;"
                "border:1px solid #A3C7AB;border-radius:6px;padding:0 4px;}"
                "QComboBox:hover{background:#B8D4BE;}"
                "QComboBox:disabled{background:#B8D4BE;color:#3A5A42;"
                "border:1px solid #A3C7AB;}"
            )
        else:
            combo_base = (
                f"QComboBox{{background:{bg};color:{fg};"
                f"border:1px solid {border};border-radius:6px;padding:0 4px;}}"
            )

        drop_down = (
            "QComboBox::drop-down{border:none;width:16px;}"
            f'QComboBox::down-arrow{{image:url("{arrow_path}");'
            "width:8px;height:6px;}"
        )

        list_style = (
            "QComboBox QAbstractItemView{"
            f"  background:{bg};"
            f"  color:{fg};"
            f"  border:1px solid {border};"
            "  outline:0;"
            "}"
            "QComboBox QAbstractItemView::item{"
            f"  background:{bg};"
            f"  color:{fg};"
            "}"
            "QComboBox QAbstractItemView::item:selected{"
            f"  background:{bg};"
            f"  color:{fg};"
            "}"
            "QComboBox QAbstractItemView::item:hover{"
            "  background:#5EA16E;"
            "  color:#FFFFFF;"
            "}"
        )

        self.app_combo.setStyleSheet(combo_base + drop_down + list_style)

    # ==================================================================
    #  座位信号（引擎推来）
    # ==================================================================

    def on_slot_changed(self, slot_name: str, value):
        if slot_name == 'alert':
            self._on_alert_changed(value)
        elif slot_name == 'status':
            self._on_status_changed(value)
        elif slot_name.startswith('switch_'):
            self._on_switch_changed(slot_name, value)

    def _on_status_changed(self, value):
        self.info_lines[0] = str(value or '')
        try:
            w = self.window()
            if w is None:
                return
            idx = w.tab_widget.indexOf(self)
            if idx >= 0 and w.info_window.current_tab == idx:
                w.info_window.set_log_lines(self.info_lines)
        except Exception:
            pass

    def _on_alert_changed(self, value):
        if value == 'blink':
            self._alert_active = True
            self._set_task_state('flash')
        elif value == 'off':
            self._alert_active = False
            if self.tab_state == 'flash':
                self._set_task_state('normal')

    def _on_switch_changed(self, slot_name: str, value):
        sw = self._switch_widgets.get(slot_name)
        if sw is None:
            return
        try:
            sw.blockSignals(True)
            sw.setChecked(bool(value))
            sw.blockSignals(False)
        except Exception:
            pass

    # ==================================================================
    #  名片下拉
    # ==================================================================

    def set_apps(self, apps):
        self.app_combo.blockSignals(True)
        self.app_combo.clear()
        for a in apps or []:
            self.app_combo.addItem(
                a.get('name', a.get('id', '')),
                a.get('id', ''))
        self.app_combo.blockSignals(False)

        if self.current_app:
            idx = self.app_combo.findData(self.current_app)
            if idx >= 0:
                self.app_combo.setCurrentIndex(idx)
        if not self.current_app and self.app_combo.count() > 0:
            self.current_app = self.app_combo.itemData(0)
            self.app_changed.emit(self.current_app)

    def _on_app_combo_changed(self, idx):
        if idx < 0:
            return
        app_id = self.app_combo.itemData(idx)
        if not app_id:
            return
        self.current_app = app_id
        self.app_changed.emit(app_id)

    def eventFilter(self, obj, event):
        if obj is self.app_combo:
            if self.device_serial:
                # 已绑定 → 吃掉所有展开动作
                etype = event.type()
                if etype in (
                    QEvent.Type.MouseButtonPress,
                    QEvent.Type.MouseButtonRelease,
                    QEvent.Type.MouseButtonDblClick,
                    QEvent.Type.KeyPress,
                    QEvent.Type.Wheel,
                ):
                    return True
            else:
                # 未绑定 → 下拉框即将展开，向引擎要一次最新列表
                if event.type() == QEvent.Type.MouseButtonPress:
                    self.request_list_apps.emit(self)
        return super().eventFilter(obj, event)

    # ==================================================================
    #  方案列表
    # ==================================================================

    def set_plugins(self, plugins):
        converted = []
        for p in plugins or []:
            converted.append(SimpleNamespace(
                task_id=p.get('task_id', ''),
                name=p.get('name', p.get('task_id', '')),
                plugin_module=p.get('task_id', ''),
                stats=list(p.get('stats', []) or []),
                default_count=p.get('default_count', 'inf'),
            ))
        self._plugins = converted
        self._reload_plugins(silent=True)

    # ==================================================================
    #  设备信息
    # ==================================================================

    def set_device_info(self, device: str, target_title: str,
                        adapter_key: str = '',
                        window_w: int = 0, window_h: int = 0):
        self.device_serial = device
        self.target_title = target_title
        self.adapter_key = adapter_key
        self.btn_bind.setText("解绑")
        self._apply_bind_button_style()
        self._apply_app_combo_style()
        self._apply_slots_ui()
        self._reload_plugins(silent=True)

    def clear_device_info(self):
        self.device_serial = None
        self.target_title = None
        self.window_hwnd = None
        self.adapter_key = None
        self.current_running_module = None
        self._alert_active = False
        self._run_timer.stop()
        self._run_start_time = None
        self.run_time_label.setText("就绪")
        self.btn_bind.setText("绑定")
        self._apply_bind_button_style()
        self._apply_app_combo_style()
        self._apply_slots_ui()
        self._set_task_state('normal')
        self._reset_func_button_state()

    # ==================================================================
    #  任务状态
    # ==================================================================

    def set_task_running(self, task_id: str):
        self.current_running_module = task_id
        if not self._alert_active:
            self._set_task_state('green')
        for btn, info in self.button_plugin_map.items():
            if info.task_id == task_id:
                self.current_func_btn = btn
                btn.set_green_background(True)
                btn.setChecked(True)
                break
        self.btn_bind.setEnabled(False)
        self.app_combo.setEnabled(False)
        self._apply_bind_button_style()
        # 归零重开
        self._run_start_time = time.monotonic()
        self._update_run_time()
        self._run_timer.start()

    def set_task_stopped(self):
        self.current_running_module = None
        if not self._alert_active:
            self._set_task_state('normal')
        self._stop_run_timer()
        self._reset_func_button_state()
        self.btn_bind.setEnabled(True)
        self.app_combo.setEnabled(True)
        self._apply_bind_button_style()

    def set_boss_state(self, state: str, detail: str = ''):
        if self._alert_active:
            return
        if state == 'running':
            self._set_task_state('green')
            self.btn_bind.setEnabled(False)
            self.app_combo.setEnabled(False)
        elif state == 'error':
            self._set_task_state('red')
            if self.current_func_btn:
                self.current_func_btn.set_red_background(True)
            self.btn_bind.setEnabled(True)
            self.app_combo.setEnabled(True)
            self._stop_run_timer()
        else:
            self.current_running_module = None
            self._set_task_state('normal')
            self._stop_run_timer()
            self._reset_func_button_state()
            self.btn_bind.setEnabled(True)
            self.app_combo.setEnabled(True)
        self._apply_bind_button_style()

    def _set_task_state(self, new_state):
        if self.tab_state != new_state:
            self.tab_state = new_state
            self._refresh_tab_bar()

    def _refresh_tab_bar(self):
        try:
            w = self.window()
            tb = getattr(w, 'tab_widget', None)
            if tb is None:
                return
            idx = tb.indexOf(self)
            if idx < 0:
                return
            bar = tb.tabBar()
            rect = bar.tabRect(idx)
            if rect.isValid():
                bar.update(rect)
        except Exception:
            pass

    # ==================================================================
    #  插件列表
    # ==================================================================

    def _reload_plugins(self, silent=False):
        if self._alert_active:
            self._alert_active = False
        if self.current_running_module:
            self._set_task_state('green')
        else:
            if self.tab_state in ('flash', 'red'):
                self._set_task_state('normal')

        if not self.current_app:
            self.scroll_list.clear_all_buttons()
            self.button_plugin_map.clear()
            return

        all_infos = list(self._plugins)

        saved_order = self._settings.get_plugin_order(self.current_app) or []
        order_map = {m: i for i, m in enumerate(saved_order)}
        sorted_infos = sorted(
            all_infos,
            key=lambda x: order_map.get(x.plugin_module, 10 ** 9))

        self.scroll_list.clear_all_buttons()
        self.button_plugin_map.clear()
        self.current_func_btn = None

        for info in sorted_infos:
            display_name = self._settings.get_display_name(info.name)
            btn = self.scroll_list.add_button(display_name)
            self.button_plugin_map[btn] = info

        if self.current_running_module:
            for btn, info in self.button_plugin_map.items():
                if info.task_id == self.current_running_module:
                    self.current_func_btn = btn
                    btn.set_green_background(True)
                    btn.setChecked(True)
                    break

        if not silent:
            self.log_message.emit('🆗 已刷新方案列表')

        self.window()._update_info_window_tabs()

    def _on_rename(self, btn, new_text: str):
        btn.set_full_text(new_text)
        info = self.button_plugin_map.get(btn)
        if info:
            self._settings.set_display_name(info.name, new_text)

    def _on_order_changed(self, new_order_texts):
        module_order = []
        for text in new_order_texts:
            for btn, info in self.button_plugin_map.items():
                if btn._full_text() == text:
                    module_order.append(info.plugin_module)
                    break
        self._settings.set_plugin_order(self.current_app, module_order)

    def _on_btn_clicked(self, btn):
        if self.current_running_module is None:
            self.current_func_btn = btn

    def _on_btn_double_clicked(self, btn):
        if not self.device_serial:
            self.append_log('❌ 请先绑定设备')
            return

        info = self.button_plugin_map.get(btn)
        if info is None:
            return

        is_running = (self.current_running_module == info.task_id)
        if is_running:
            self.request_stop_task.emit(self)
        else:
            if self.current_running_module is not None:
                if self.current_func_btn:
                    self.current_func_btn.shake()
                return
            text = self.count_input.text().strip().lower()
            if text == 'inf':
                self.max_count = float('inf')
            else:
                try:
                    v = int(text)
                    if v <= 0:
                        self.append_log('❌ 次数必须大于0')
                        return
                    self.max_count = v
                except ValueError:
                    self.append_log('❌ 请输入有效次数或inf')
                    return

            self.current_func_btn = btn
            self.request_start_task.emit(self, info.task_id)

    def _reset_count_input(self):
        self.count_input.setText("inf")

    # ==================================================================
    #  运行时间
    # ==================================================================

    def _update_run_time(self):
        if self._run_start_time is None:
            return
        t = int(time.monotonic() - self._run_start_time)
        self.run_time_label.setText(
            f"⏳ 运行 {t // 3600:02d}:{t % 3600 // 60:02d}:{t % 60:02d}")

    def _stop_run_timer(self):
        self._run_timer.stop()
        if self._run_start_time is None:
            return
        t = int(time.monotonic() - self._run_start_time)
        self._run_start_time = None
        self.run_time_label.setText(
            f"⌛ 运行 {t // 3600:02d}:{t % 3600 // 60:02d}:{t % 60:02d}")

    # ==================================================================
    #  按钮状态辅助
    # ==================================================================

    def _reset_func_button_state(self):
        if self.current_func_btn:
            try:
                self.current_func_btn.reset_style()
            except Exception:
                pass
        self.current_func_btn = None

    # ==================================================================
    #  日志
    # ==================================================================

    def append_log(self, msg: str):
        try:
            self.log_view.appendPlainText(msg)
        except Exception:
            return
        try:
            lines = self.info_lines
            if '统计' in msg or '当前脚本' in msg:
                lines[1] = msg
            else:
                for i in range(2, 6):
                    lines[i] = lines[i + 1]
                lines[6] = msg
        except Exception:
            pass

    def get_info_lines(self):
        return self.info_lines

    def clear_log(self):
        self.log_view.clear()

    # ==================================================================
    #  主题
    # ==================================================================

    def toggle_theme(self, checked: bool):
        for btn in self.scroll_list.buttons:
            if not (self.current_running_module and btn is self.current_func_btn):
                btn.reset_style()
        if self.current_running_module and self.current_func_btn:
            self.current_func_btn.set_green_background(True)

        self._apply_bind_button_style()
        self._apply_clear_log_style()
        self._apply_refresh_style()
        self._apply_count_reset_style()
        self._apply_scrollbar_style()
        self._apply_app_combo_style()

        colors = DARK_COLORS if checked else LIGHT_COLORS
        self.run_time_label.setStyleSheet(
            f"QLabel {{ color: {colors['text']}; font-size: 9pt; "
            f"background: transparent; padding-right: 4px; }}")

        for sw in self._switch_widgets.values():
            try:
                sw.set_dark_mode(checked)
            except Exception:
                pass