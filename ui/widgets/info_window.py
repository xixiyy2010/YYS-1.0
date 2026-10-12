# widgets/info_window.py
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel
from PyQt6.QtCore import Qt, pyqtSignal, QTimer
from PyQt6.QtGui import QPainter, QColor


class ClickableLabel(QLabel):
    clicked = pyqtSignal(int)
    double_clicked = pyqtSignal(int)

    def __init__(self, text, index, parent=None):
        super().__init__(text, parent)
        self.index = index
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)

        font = self.font()
        font.setPointSize(10)
        self.setFont(font)

        fm = self.fontMetrics()
        # 框紧贴文字：左右各 4px，上下各 2px
        w = fm.horizontalAdvance(text) + 8
        h = fm.height() + 4
        self.setFixedSize(w, h)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.index)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.double_clicked.emit(self.index)
            event.accept()
            return
        super().mouseDoubleClickEvent(event)


class InfoWindow(QWidget):
    """
    悬浮窗

    改动要点（v2）：
        - update_tabs 开头先停 + 清所有闪烁定时器（flash_timers /
          flash_base_colors）。原来只清 color_types，导致：
            1) 旧 timer 继续跑，误改重建后的新标签颜色
            2) 每个 slot_changed 事件重建标签 → 持续泄漏 timer
    """
    tab_clicked = pyqtSignal(int)
    tab_double_clicked = pyqtSignal(int)   

    def __init__(self, tab_count=1):
        super().__init__()
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)

        self.tab_count = tab_count
        self.current_tab = 0
        self.flash_timers = {}       # index -> QTimer
        self.flash_base_colors = {}  # index -> base color type ('normal','green','red')
        self.is_dark = False
        self.color_types = {}        # index -> current color type

        self.main_layout = QVBoxLayout(self)
        self.main_layout.setContentsMargins(1, 1, 1, 1)
        self.main_layout.setSpacing(1)

        self.tab_layout = QHBoxLayout()
        self.tab_layout.setSpacing(2)
        self.tabs = []
        self.update_tabs()
        self.main_layout.addLayout(self.tab_layout)

        self.log_lines = []
        for _ in range(7):
            label = QLabel("")
            label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
            label.setStyleSheet("color: white; font-size: 9pt;")
            label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
            # 跟上面标签框内的文字左对齐
            label.setIndent(4)
            # 锁定最小高度，layout 不能压缩它
            _lfm = label.fontMetrics()
            label.setMinimumHeight(_lfm.height() + 2)
            self.log_lines.append(label)
            self.main_layout.addWidget(label)

        fm = self.fontMetrics()
        char_width = fm.horizontalAdvance("中") if fm.horizontalAdvance("中") > 0 else fm.horizontalAdvance("W")
        self.setFixedWidth(char_width * 15 + 10)
        self.setFixedHeight(7 * fm.height() + 50)

        self.select_tab(0)
        self.set_log_lines([
            "悬赏协助：接受：0 拒绝：0",
            "当前脚本：XX：X XX：X",
            "实时日志1",
            "实时日志2",
            "实时日志3",
            "实时日志4",
            "实时日志5"
        ])

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        if self.is_dark:
            bg_color = QColor(0, 0, 0, 150)
        else:
            bg_color = QColor(255, 255, 255, 120)

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(bg_color)
        painter.drawRoundedRect(self.rect(), 10, 10)

    def apply_theme(self, is_dark):
        self.is_dark = is_dark
        text_color = "white" if is_dark else "#000000"
        for label in self.log_lines:
            label.setStyleSheet(f"color: {text_color}; font-size: 9pt;")
        for i in range(len(self.tabs)):
            self._apply_tab_style(i, selected=(i == self.current_tab))
        self.update()

    def _apply_tab_style(self, index, selected=False):
        """根据当前颜色类型和选中状态更新标签样式"""
        if index >= len(self.tabs):
            return
        color_type = self.color_types.get(index, 'normal')
        if color_type == 'green':
            bg = "background-color: rgba(0, 180, 0, 0.9);"
        elif color_type == 'red':
            bg = "background-color: rgba(200, 0, 0, 0.9);"
        else:
            bg = "background-color: rgba(128, 128, 128, 0.8);"
        border = "2px solid white;" if selected else "2px solid transparent;"
        self.tabs[index].setStyleSheet(
            f"QLabel {{ {bg} color: white; border-radius: 5px; border: {border} }}"
        )

    def update_tabs(self, tab_names=None):
        # 先停掉所有闪烁定时器：
        #   1) 避免旧 timer 继续跑，误改重建后的新标签颜色
        #   2) 避免持续泄漏（每个 slot_changed 事件都会重建标签）
        for timer in list(self.flash_timers.values()):
            try:
                timer.stop()
                timer.deleteLater()
            except Exception:
                pass
        self.flash_timers.clear()
        self.flash_base_colors.clear()

        # 清除旧标签
        while self.tab_layout.count():
            item = self.tab_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.tabs.clear()
        self.color_types.clear()

        for i in range(self.tab_count):
            text = ''
            if tab_names and i < len(tab_names):
                text = tab_names[i] or str(i + 1)
            else:
                text = str(i + 1)
            label = ClickableLabel(text, i)
            label.clicked.connect(self.select_tab)
            label.double_clicked.connect(self._on_label_double_clicked)
            label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
            self.tabs.append(label)
            self.tab_layout.addWidget(label)
            self.color_types[i] = 'normal'
            self._apply_tab_style(i, selected=(i == self.current_tab))

        self.tab_layout.addStretch(1)

    def _on_label_double_clicked(self, index):
        self.tab_double_clicked.emit(index)

    def select_tab(self, index):
        if index >= len(self.tabs):
            return
        self.current_tab = index
        for i in range(len(self.tabs)):
            self._apply_tab_style(i, selected=(i == index))
        self.tab_clicked.emit(index)

    def sync_selected_tab(self, index):
        if index >= len(self.tabs):
            return
        self.current_tab = index
        for i in range(len(self.tabs)):
            self._apply_tab_style(i, selected=(i == index))

    def set_log_lines(self, lines):
        for label, text in zip(self.log_lines, lines):
            label.setText(text)
        for i in range(len(lines), len(self.log_lines)):
            self.log_lines[i].setText("")

    def update_tab_color(self, index, color_type='normal'):
        """更新指定标签的颜色类型，若为 flash 则启动闪烁"""
        if index >= len(self.tabs):
            return
        self.color_types[index] = color_type
        if color_type == 'flash':
            self.start_flash(index)
            return
        # 停止可能存在的闪烁并恢复基础颜色
        if index in self.flash_timers:
            self.stop_flash(index)
        self._apply_tab_style(index, selected=(index == self.current_tab))

    def start_flash(self, index):
        """启动标签闪烁，保存基础颜色"""
        if index >= len(self.tabs):
            return
        # 保存基础颜色（如果当前不是 flash）
        if self.color_types.get(index) != 'flash':
            self.flash_base_colors[index] = self.color_types.get(index, 'normal')
        # 停止已有闪烁
        if index in self.flash_timers:
            self.flash_timers[index].stop()
            self.flash_timers[index].deleteLater()
        # 启动新闪烁
        timer = QTimer(self)
        timer.setInterval(500)
        timer.timeout.connect(lambda idx=index: self.toggle_flash_color(idx))
        timer.start()
        self.flash_timers[index] = timer
        self.color_types[index] = 'flash'

    def stop_flash(self, index):
        """停止闪烁并恢复基础颜色"""
        if index in self.flash_timers:
            timer = self.flash_timers.pop(index)
            timer.stop()
            timer.deleteLater()
        # 恢复基础颜色
        if index in self.flash_base_colors:
            base = self.flash_base_colors.pop(index)
        else:
            base = 'normal'
        self.color_types[index] = base
        self._apply_tab_style(index, selected=(index == self.current_tab))

    def toggle_flash_color(self, index):
        """闪烁时切换绿色/红色（仅用于内部计时器）"""
        if index >= len(self.tabs):
            return
        current = self.color_types.get(index, 'normal')
        if current == 'green':
            self.color_types[index] = 'red'
        else:
            self.color_types[index] = 'green'
        self._apply_tab_style(index, selected=(index == self.current_tab))

    def reposition(self):
        from PyQt6.QtWidgets import QApplication
        screen = QApplication.primaryScreen()
        if screen:
            available = screen.availableGeometry()
            self.move(available.right() - self.width(),
                      available.bottom() - self.height() - 30)