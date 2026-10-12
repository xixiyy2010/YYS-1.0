# ui/main_window.py
"""
MainWindow —— 主窗口（纯 UI 层，通过 engine_client 和引擎通信）

卷帘动画（顶边固定）：
    展开态：320×432，全是内容，画轴控件不存在。
    卷起：窗口高度 432 → 16，动态创建画轴贴底，边卷边往上跑。
    卷完：画轴从窗口底部飞到系统托盘，边飞边缩，最后隐藏。
    恢复：画轴从托盘飞回展开位，然后窗口高度 16 → 432，画轴被顶掉。

状态机（_phase）：
    idle     展开态，稳定
    rolling  窗口高度动画中（方向由动画终点决定）
    flying   窗口飞向/飞离托盘中
    hidden   已隐藏，只剩托盘图标

跨线程规则：
    所有从 EngineClient 来的信号都用 QueuedConnection 连到主线程槽。
    后台线程只 emit 信号，绝不碰任何 UI 部件。

依赖：
    contracts.ui_protocol / ui.* / styles / settings
"""
import ctypes
import os

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QTabWidget, QSystemTrayIcon, QMenu, QWidget,
    QVBoxLayout, QInputDialog, QLabel,
)
from PyQt6.QtGui import QIcon, QAction, QColor, QPainter
from PyQt6.QtCore import (
    Qt, QTimer, QPropertyAnimation, QEasingCurve, pyqtProperty,
)

from contracts.ui_protocol import UICommands, UIEvents
from ui.custom_tab_bar import CustomTabBar
from ui.title_bar import TitleBar
from ui.widgets import DeviceTab, InfoWindow
from ui.widgets.roller_blind import RollerBar
from styles import LIGHT_STYLE, DARK_STYLE
from settings import Settings


def _get_tray_notify_center():
    """实时拿系统托盘通知区屏幕中心。任务栏换边/换屏都跟得上。"""
    try:
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        user32.FindWindowW.restype = wintypes.HWND
        user32.FindWindowExW.restype = wintypes.HWND

        hwnd_tray = user32.FindWindowW("Shell_TrayWnd", None)
        if not hwnd_tray:
            return None
        hwnd_notify = user32.FindWindowExW(
            hwnd_tray, 0, "TrayNotifyWnd", None)
        if not hwnd_notify:
            return None

        rect = wintypes.RECT()
        if not user32.GetWindowRect(hwnd_notify, ctypes.byref(rect)):
            return None
        return ((rect.left + rect.right) // 2,
                (rect.top + rect.bottom) // 2)
    except Exception:
        return None


class MainWindow(QMainWindow):

    # ==================================================================
    #  动画驱动属性
    # ==================================================================

    # ---- 窗口高度（卷轴）----

    def _get_roll_window_height(self):
        return self._roll_window_h

    def _set_roll_window_height(self, h):
        h = float(h)
        if abs(h - self._roll_window_h) < 0.01:
            return
        self._roll_window_h = h
        self._apply_roll_state(int(h))

    roll_window_height = pyqtProperty(
        float, fget=_get_roll_window_height, fset=_set_roll_window_height)

    # ---- 飞托盘进度（0=起点, 1=终点）----

    def _get_fly_progress(self):
        return self._fly_progress

    def _set_fly_progress(self, v):
        v = float(v)
        self._fly_progress = v
        sx, sy, sw, sh = self._fly_start
        ex, ey, ew, eh = self._fly_end
        self.setFixedSize(max(1, int(sw + (ew - sw) * v)),
                          max(1, int(sh + (eh - sh) * v)))
        self.move(int(sx + (ex - sx) * v),
                  int(sy + (ey - sy) * v))

    fly_progress = pyqtProperty(
        float, fget=_get_fly_progress, fset=_set_fly_progress)

    # ==================================================================

    def __init__(self, engine_client, settings: Settings):
        super().__init__()

        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

        self._engine = engine_client
        self._settings = settings

        self._quitting = False
        self._engine_available = False
        self._apps = []
        self._pending_candidates = None

        self._save_pos_timer = QTimer(self)
        self._save_pos_timer.setSingleShot(True)
        self._save_pos_timer.setInterval(500)
        self._save_pos_timer.timeout.connect(self._do_save_pos)

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint | Qt.WindowType.Window)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

        # ---- 尺寸 ----
        self._content_h = 432
        self._roller_h = RollerBar.HEIGHT
        self._roll_window_h = float(self._content_h)

        self.roller_bar = None           # 展开态下不存在

        # ---- 状态机 ----
        self._phase = 'idle'             # idle / rolling / flying / hidden

        # ---- 动画中间态 ----
        self._roll_done = None
        self._fly_done = None
        self._fly_progress = 0.0
        self._fly_start = (0, 0, 320, self._roller_h)
        self._fly_end = (0, 0, 320, self._roller_h)
        self._suppress_pos_save = False

        self.setFixedWidth(320)
        self.setFixedHeight(self._content_h)

        self._build_ui()

        # ---- 两个动画 ----
        self._roll_anim = QPropertyAnimation(
            self, b"roll_window_height", self)
        self._roll_anim.setDuration(260)
        self._roll_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._roll_anim.finished.connect(self._on_roll_finished)

        self._fly_anim = QPropertyAnimation(
            self, b"fly_progress", self)
        self._fly_anim.setDuration(260)
        self._fly_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._fly_anim.setStartValue(0.0)
        self._fly_anim.setEndValue(1.0)
        self._fly_anim.finished.connect(self._on_fly_finished)

        # ---- 引擎门铃 ----
        self._engine.sig_event.connect(
            self._on_engine_event_ui, Qt.ConnectionType.QueuedConnection)
        self._engine.sig_connected.connect(
            self._on_engine_connected_ui, Qt.ConnectionType.QueuedConnection)
        self._engine.sig_disconnected.connect(
            self._on_engine_lost_ui, Qt.ConnectionType.QueuedConnection)

        self._apply_tooltip_style(False)
        self._add_new_tab()

        if self._engine.is_connected():
            self._on_engine_connected_ui()
        else:
            self._on_engine_lost_ui()

    # ==================================================================
    #  UI 构建
    # ==================================================================

    def _build_ui(self):
        self._load_window_position()

        self._central = QWidget(self)
        self._central.setObjectName("centralContainer")
        self._central.setStyleSheet("background: transparent;")
        self.setCentralWidget(self._central)

        # 内容：固定 320×432，直接挂 central。
        # 窗口变矮时，超出窗口下沿的部分由 Qt 自动裁掉。
        self.content = QWidget(self._central)
        self.content.setFixedSize(self.width(), self._content_h)
        self.content.move(0, 0)

        content_layout = QVBoxLayout(self.content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(0)

        self.title_bar = TitleBar(self.content)
        self.title_bar.minimize_clicked.connect(self.hide_to_tray)
        self.title_bar.close_clicked.connect(self.quit_app)
        self.title_bar.theme_changed.connect(self.toggle_theme)
        content_layout.addWidget(self.title_bar)

        self.tab_widget = QTabWidget()
        self.tab_widget.setTabBar(CustomTabBar())
        self.tab_widget.setTabsClosable(False)
        self.tab_widget.setDocumentMode(True)
        self.tab_widget.setMovable(False)
        content_layout.addWidget(self.tab_widget, 1)

        self.info_window = InfoWindow(tab_count=0)
        self.info_window.hide()
        self.is_dark_theme = False
        self.info_window.tab_clicked.connect(self.on_info_tab_clicked)
        self.info_window.tab_double_clicked.connect(self.on_info_tab_double_clicked)

        self.tab_widget.tabBar().plus_clicked.connect(self._add_new_tab)
        self.tab_widget.currentChanged.connect(self.on_tab_changed)
        self.tab_widget.tabBar().tab_double_clicked.connect(self.on_tab_double_clicked)

        _icon_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            'icon.ico')
        self.tray_icon = QSystemTrayIcon(QIcon(_icon_path), self)
        self.tray_icon.setToolTip("痒痒鼠")
        tray_menu = QMenu()
        show_action = QAction("显示主窗口", self)
        show_action.triggered.connect(self.show_window)
        quit_action = QAction("退出", self)
        quit_action.triggered.connect(self.quit_app)
        tray_menu.addAction(show_action)
        tray_menu.addAction(quit_action)
        self.tray_icon.setContextMenu(tray_menu)
        self.tray_icon.activated.connect(self.on_tray_activated)
        self.tray_icon.show()

        self.setStyleSheet(LIGHT_STYLE)
        self.info_window.apply_theme(False)

        self._lost_label = QLabel("⚠ 引擎失联\n\n请重启程序", self.content)
        self._lost_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._lost_label.setStyleSheet(
            "QLabel {"
            "  background: rgba(0, 0, 0, 170);"
            "  color: #FFFFFF;"
            "  font-size: 12pt;"
            "  border-radius: 12px;"
            "  padding: 20px;"
            "}"
        )
        self._lost_label.hide()

    # ==================================================================
    #  卷帘：画轴控件 + 窗口高度状态
    # ==================================================================

    def _ensure_roller_bar(self):
        if self.roller_bar is None:
            self.roller_bar = RollerBar(self._central)
            self.roller_bar.setFixedWidth(self.width())
            self.roller_bar.apply_theme(self.is_dark_theme)
            self.roller_bar.show()
        return self.roller_bar

    def _destroy_roller_bar(self):
        if self.roller_bar is not None:
            self.roller_bar.hide()
            self.roller_bar.deleteLater()
            self.roller_bar = None

    def _apply_roll_state(self, win_h):
        """窗口高度变化时重排画轴。顶边固定（setFixedHeight 不动 y）。"""
        if win_h >= self._content_h:
            self._destroy_roller_bar()
            self.setFixedHeight(self._content_h)
            return

        bar = self._ensure_roller_bar()
        bar.move(0, win_h - self._roller_h)
        self.setFixedHeight(win_h)

        denom = self._content_h - self._roller_h
        progress = (self._content_h - win_h) / denom if denom > 0 else 1.0
        bar.set_paper_thickness(progress)

    # ==================================================================
    #  两个通用动画
    # ==================================================================

    def _roll_to(self, target_h, on_done):
        """窗口高度：当前 → target_h。"""
        self._roll_done = on_done
        self._roll_anim.stop()
        self._roll_anim.setStartValue(float(self._roll_window_h))
        self._roll_anim.setEndValue(float(target_h))
        self._roll_anim.start()

    def _fly_to(self, to_rect, on_done):
        """窗口：当前位置/尺寸 → to_rect(x,y,w,h)。"""
        self._fly_start = (self.x(), self.y(), self.width(), self.height())
        self._fly_end = to_rect
        self._fly_progress = 0.0
        self._suppress_pos_save = True
        self._fly_done = on_done
        self._fly_anim.stop()
        self._fly_anim.start()

    def _on_roll_finished(self):
        # 动画值可能差零点几没到终点，强制对齐
        end = float(self._roll_anim.endValue())
        self._roll_window_h = end
        self._apply_roll_state(int(end))
        cb, self._roll_done = self._roll_done, None
        if cb:
            cb()

    def _on_fly_finished(self):
        self._suppress_pos_save = False
        cb, self._fly_done = self._fly_done, None
        if cb:
            cb()

    # ==================================================================
    #  托盘入口：最小化 / 恢复
    # ==================================================================

    def hide_to_tray(self):
        if self._phase != 'idle':
            return
        self._save_window_position()
        if self.windowState() & Qt.WindowState.WindowMinimized:
            self.showNormal()
        self._phase = 'rolling'
        self._roll_to(self._roller_h, self._after_roll_up)

    def show_window(self):
        self._quitting = False

        if self._phase == 'idle':
            self.showNormal()
            self.raise_()
            self.activateWindow()
            return

        if self._phase != 'hidden':
            return

        self.info_window.hide()
        self._phase = 'flying'
        self._suppress_pos_save = True
        tx, ty = self._tray_point()
        self.setFixedSize(6, 6)
        self.move(tx, ty)
        self.showNormal()
        ex, ey = self._saved_xy()
        self._fly_to((ex, ey, 320, self._roller_h), self._after_fly_in)

    # ==================================================================
    #  动画链
    # ==================================================================

    def _after_roll_up(self):
        """卷起完成 → 飞向托盘。"""
        self._phase = 'flying'
        tx, ty = self._tray_point()
        self._fly_to((tx, ty, 6, 6), self._after_fly_out)

    def _after_fly_out(self):
        """飞到托盘 → 隐藏 → 显示悬浮窗。"""
        self._phase = 'hidden'
        self.hide()
        self.info_window.show()
        self.info_window.reposition()

    def _after_fly_in(self):
        """从托盘飞回展开位 → 展开。"""
        self._phase = 'rolling'
        self._roll_to(self._content_h, self._after_roll_down)

    def _after_roll_down(self):
        """展开完成 → 回到稳定态。"""
        self._phase = 'idle'

    # ==================================================================
    #  位置辅助
    # ==================================================================

    def _tray_point(self):
        pt = _get_tray_notify_center()
        if pt is not None:
            return pt
        avail = QApplication.primaryScreen().availableGeometry()
        return avail.right() - 30, avail.bottom() - 10

    def _saved_xy(self):
        pos = self._settings.data.get('window_pos')
        if pos and isinstance(pos, list) and len(pos) == 2:
            return pos[0], pos[1]
        return self.x(), self.y()

    # ==================================================================
    #  Tab 管理
    # ==================================================================

    def _add_new_tab(self):
        index = self.tab_widget.count() + 1
        tab = DeviceTab(index, self._settings, self)
        tab.display_name = None
        tab.set_apps(self._app_choices())

        tab.log_message.connect(self._on_tab_log_message)
        tab.app_changed.connect(self._on_tab_app_changed)
        tab.bind_clicked.connect(self._on_tab_bind_clicked)
        tab.slot_command.connect(self._on_tab_slot_command)
        tab.clear_log_clicked.connect(self._on_tab_clear_log_clicked)
        tab.request_start_task.connect(self._on_tab_request_start_task)
        tab.request_stop_task.connect(self._on_tab_request_stop_task)
        tab.request_list_apps.connect(self._on_tab_request_list_apps)

        new_index = self.tab_widget.addTab(tab, "未绑定")
        self.tab_widget.setCurrentIndex(new_index)

        if getattr(tab, 'current_app', None):
            self._apply_app_to_tab(tab, tab.current_app)

        self.on_tab_changed(new_index)
        self._update_info_window_tabs()

    def _app_choices(self):
        return [
            {'id': a.get('id', ''),
             'name': a.get('name', a.get('id', ''))}
            for a in self._apps
        ]

    def _find_app_tasks(self, app_id):
        for a in self._apps:
            if a.get('id') == app_id:
                return list(a.get('tasks', []) or [])
        return []

    def _find_app_slots(self, app_id):
        for a in self._apps:
            if a.get('id') == app_id:
                return dict(a.get('slots', {}) or {})
        return {}

    def _apply_app_to_tab(self, tab, app_id):
        try:
            tab.set_plugins(self._find_app_tasks(app_id))
            tab.set_slots(self._find_app_slots(app_id))
            tab._reload_plugins(silent=True)
        except Exception:
            pass

    def _set_tab_app(self, tab, app_id: str):
        if not app_id:
            return
        if getattr(tab, 'current_app', None) == app_id:
            self._apply_app_to_tab(tab, app_id)
            return
        try:
            idx = tab.app_combo.findData(app_id)
            if idx >= 0:
                tab.app_combo.blockSignals(True)
                tab.app_combo.setCurrentIndex(idx)
                tab.app_combo.blockSignals(False)
                tab.current_app = app_id
                self._apply_app_to_tab(tab, app_id)
        except Exception:
            pass

    def _find_app_name(self, app_id):
        for a in self._apps:
            if a.get('id') == app_id:
                return a.get('name', app_id)
        return app_id

    def _is_same_business(self, display_name, app_name):
        if not display_name or not app_name:
            return False
        if display_name == app_name:
            return True
        if display_name.startswith(app_name):
            suffix = display_name[len(app_name):]
            if suffix.isdigit():
                return True
        return False

    def _assign_tab_title(self, tab, app_id):
        app_name = self._find_app_name(app_id)
        current = getattr(tab, 'display_name', None)

        if self._is_same_business(current, app_name):
            return

        used = set()
        for i in range(self.tab_widget.count()):
            other = self.tab_widget.widget(i)
            if other is tab:
                continue
            name = getattr(other, 'display_name', None)
            if name:
                used.add(name)

        if app_name not in used:
            display_name = app_name
        else:
            n = 1
            while f'{app_name}{n}' in used:
                n += 1
            display_name = f'{app_name}{n}'

        tab.display_name = display_name
        idx = self.tab_widget.indexOf(tab)
        if idx >= 0:
            self.tab_widget.setTabText(idx, display_name)

    def _release_tab_title(self, tab):
        tab.display_name = None
        idx = self.tab_widget.indexOf(tab)
        if idx >= 0:
            self.tab_widget.setTabText(idx, "未绑定")

    def _find_empty_tab(self):
        for i in range(self.tab_widget.count()):
            tab = self.tab_widget.widget(i)
            if tab is not None and not getattr(tab, 'device_serial', None):
                return tab
        return None

    def _find_tab_by_device(self, device: str):
        for i in range(self.tab_widget.count()):
            tab = self.tab_widget.widget(i)
            if tab is not None and getattr(tab, 'device_serial', None) == device:
                return tab
        return None

    def delete_tab(self, index):
        if not (0 <= index < self.tab_widget.count()):
            return
        widget = self.tab_widget.widget(index)
        if getattr(widget, 'device_serial', None):
            return
        if self.tab_widget.count() <= 1:
            return
        self.tab_widget.removeTab(index)
        widget.deleteLater()
        self._renumber_tabs()
        self._update_info_window_tabs()

    def _renumber_tabs(self):
        for i in range(self.tab_widget.count()):
            tab = self.tab_widget.widget(i)
            tab.index = i + 1
            display = getattr(tab, 'display_name', None)
            if display:
                self.tab_widget.setTabText(i, display)
            else:
                self.tab_widget.setTabText(i, "未绑定")

    def _update_info_window_tabs(self):
        self.info_window.tab_count = self.tab_widget.count()
        names = []
        for i in range(self.tab_widget.count()):
            tab = self.tab_widget.widget(i)
            if tab is None:
                names.append('')
                continue
            name = getattr(tab, 'display_name', None) or '未绑定'
            names.append(name)
        self.info_window.update_tabs(names)
        for i in range(self.tab_widget.count()):
            tab = self.tab_widget.widget(i)
            if tab is None:
                continue
            state = getattr(tab, 'tab_state', 'normal')
            if state == 'flash':
                self.info_window.stop_flash(i)
                self.info_window.start_flash(i)
            else:
                self.info_window.update_tab_color(i, state)

    def on_tab_changed(self, index):
        self._update_info_window_tabs()
        self.info_window.sync_selected_tab(index)
        tab = self.tab_widget.widget(index)
        if tab:
            self.info_window.set_log_lines(tab.get_info_lines())

    # ==================================================================
    #  引擎事件
    # ==================================================================

    def _on_engine_event_ui(self, name: str, data: dict):
        if self._quitting:
            return
        if name == UIEvents.SYNC_RESULT:
            self._handle_sync_result(data)
        elif name == UIEvents.CANDIDATES_RESULT:
            self._handle_candidates_result(data)
        elif name == UIEvents.DEVICE_CONNECTED:
            self._handle_device_connected(data)
        elif name == UIEvents.DEVICE_DISCONNECTED:
            self._handle_device_disconnected(data)
        elif name == UIEvents.BOSS_STATE:
            self._handle_boss_state(data)
        elif name == UIEvents.SLOT_CHANGED:
            self._handle_slot_changed(data)
        elif name == UIEvents.APPS_UPDATED:
            self._handle_apps_updated(data)
        elif name == UIEvents.LOG:
            self._handle_log(data)

    def _on_engine_connected_ui(self):
        self._engine_available = True
        self._lost_label.hide()
        self.tab_widget.setEnabled(True)
        self._engine.send(UICommands.SYNC, {})

    def _on_engine_lost_ui(self):
        self._engine_available = False
        self._lost_label.resize(self.content.size())
        self._lost_label.move(0, 0)
        self._lost_label.show()
        self._lost_label.raise_()
        self.tab_widget.setEnabled(False)

    def _handle_sync_result(self, data: dict):
        apps = data.get('apps', []) or []
        devices = data.get('devices', []) or []
        self._apps = apps

        choices = self._app_choices()
        for i in range(self.tab_widget.count()):
            tab = self.tab_widget.widget(i)
            if tab is None:
                continue
            tab.set_apps(choices)

        for dev in devices:
            device = dev.get('device', '')
            if not device:
                continue

            tab = self._find_tab_by_device(device)
            if tab is None:
                tab = self._find_empty_tab()
                if tab is None:
                    self._add_new_tab()
                    tab = self._find_empty_tab()
                if tab is None:
                    continue

            self._set_tab_app(tab, dev.get('app_id', ''))
            tab.set_device_info(
                device=device,
                target_title=dev.get('target_title', ''),
                adapter_key=dev.get('adapter_key', ''),
                window_w=dev.get('window_w', 0),
                window_h=dev.get('window_h', 0),
            )
            self._assign_tab_title(tab, dev.get('app_id', ''))

            running_task = dev.get('running_task_id', '') or ''
            if running_task:
                try:
                    tab.set_task_running(running_task)
                except Exception:
                    pass

            state = dev.get('state', 'idle')
            if state and state != 'idle':
                try:
                    tab.set_boss_state(state, dev.get('detail', ''))
                except Exception:
                    pass

        self._update_info_window_tabs()

    def _handle_candidates_result(self, data: dict):
        pending = self._pending_candidates
        self._pending_candidates = None
        if pending is None:
            return

        tab = pending['tab']
        candidates = data.get('candidates', []) or []
        if not candidates:
            try:
                tab.append_log('❌ 未找到可用的目标窗口')
            except Exception:
                pass
            return

        titles = [
            f"{c.get('target_title', '')}  [{c.get('adapter_key', '')}]"
            for c in candidates
        ]

        if len(candidates) == 1:
            selected_title = candidates[0].get('target_title', '')
        else:
            item, ok = QInputDialog.getItem(
                self, "选择设备", "请选择要绑定的目标窗口：",
                titles, 0, False)
            if not ok or not item:
                return
            idx = titles.index(item)
            selected_title = candidates[idx].get('target_title', '')

        self._engine.send(UICommands.BIND, {
            'target_title': selected_title,
            'app_id': pending['app_id'],
        })

    def _handle_device_connected(self, data: dict):
        device = data.get('device', '')
        if not device:
            return

        existing = self._find_tab_by_device(device)
        if existing is not None:
            self._set_tab_app(existing, data.get('app_id', ''))
            existing.set_device_info(
                device=device,
                target_title=data.get('target_title', ''),
                adapter_key=data.get('adapter_key', ''),
                window_w=data.get('window_w', 0),
                window_h=data.get('window_h', 0),
            )
            self._assign_tab_title(existing, data.get('app_id', ''))
            return

        tab = self._find_empty_tab()
        if tab is None:
            self._add_new_tab()
            tab = self._find_empty_tab()
        if tab is not None:
            self._set_tab_app(tab, data.get('app_id', ''))
            tab.set_device_info(
                device=device,
                target_title=data.get('target_title', ''),
                adapter_key=data.get('adapter_key', ''),
                window_w=data.get('window_w', 0),
                window_h=data.get('window_h', 0),
            )
            self._assign_tab_title(tab, data.get('app_id', ''))

    def _handle_device_disconnected(self, data: dict):
        device = data.get('device', '')
        if not device:
            return
        tab = self._find_tab_by_device(device)
        if tab is not None:
            self._release_tab_title(tab)
            tab.clear_device_info()
        self._update_info_window_tabs()

    def _handle_boss_state(self, data: dict):
        device = data.get('device', '')
        state = data.get('state', '')
        detail = data.get('detail', '')
        tab = self._find_tab_by_device(device)
        if tab is None:
            return
        try:
            tab.set_boss_state(state, detail)
        except Exception:
            pass
        self._update_info_window_tabs()

    def _handle_slot_changed(self, data: dict):
        device = data.get('device', '')
        slot_name = data.get('slot', '')
        value = data.get('value')
        tab = self._find_tab_by_device(device)
        if tab is None:
            return
        try:
            tab.on_slot_changed(slot_name, value)
        except Exception:
            pass
        self._update_info_window_tabs()

    def _handle_log(self, data: dict):
        device = data.get('device', '')
        message = data.get('message', '')

        target = None
        if device:
            target = self._find_tab_by_device(device)
        if target is None:
            target = self.tab_widget.currentWidget()
        if target is None:
            return

        try:
            target.append_log(message)
        except Exception:
            pass

        try:
            idx = self.tab_widget.indexOf(target)
            if idx >= 0 and self.info_window.current_tab == idx:
                self.info_window.set_log_lines(target.get_info_lines())
        except Exception:
            pass

    # ==================================================================
    #  Tab 事件
    # ==================================================================

    def _on_tab_log_message(self, msg: str):
        tab = self.sender()
        if tab is None:
            return
        tab.append_log(msg)

    def _on_tab_app_changed(self, app_id: str):
        tab = self.sender()
        if tab is None:
            return
        self._apply_app_to_tab(tab, app_id)

    def _on_tab_bind_clicked(self, tab):
        device = getattr(tab, 'device_serial', None)

        if device:
            if getattr(tab, 'current_running_module', None):
                tab.append_log('❌ 请先停止任务再解绑')
                return
            self._engine.send(UICommands.UNBIND, {'device': device})
            return

        app_id = getattr(tab, 'current_app', None)
        if not app_id:
            tab.append_log('❌ 请先选择业务')
            return

        self._pending_candidates = {'tab': tab, 'app_id': app_id}
        self._engine.send(UICommands.LIST_CANDIDATES, {'app_id': app_id})

    def _on_tab_request_list_apps(self, tab):
        self._engine.send(UICommands.LIST_APPS, {})

    def _handle_apps_updated(self, data: dict):
        apps = data.get('apps', []) or []
        self._apps = apps
        choices = self._app_choices()
        for i in range(self.tab_widget.count()):
            tab = self.tab_widget.widget(i)
            if tab is not None:
                tab.set_apps(choices)

    def _on_tab_slot_command(self, tab, slot_name: str, value):
        device = getattr(tab, 'device_serial', None)
        if not device:
            return
        self._engine.send(UICommands.SLOT_COMMAND, {
            'device': device,
            'slot': slot_name,
            'value': value,
        })

    def _on_tab_clear_log_clicked(self, tab):
        try:
            tab.clear_log()
        except Exception:
            pass

    def _on_tab_request_start_task(self, tab, task_id: str):
        device = getattr(tab, 'device_serial', None)
        if not device:
            tab.append_log('❌ 请先绑定设备')
            return

        max_count = getattr(tab, 'max_count', float('inf'))
        if max_count == float('inf'):
            count_str = 'inf'
        else:
            try:
                count_str = str(int(max_count))
            except Exception:
                count_str = 'inf'

        tab.set_task_running(task_id)
        self._engine.send(UICommands.START_TASK, {
            'device': device,
            'task_id': task_id,
            'max_count': count_str,
        })

    def _on_tab_request_stop_task(self, tab):
        device = getattr(tab, 'device_serial', None)
        if device:
            self._engine.send(UICommands.STOP_TASK, {'device': device})
        tab.set_task_stopped()

    # ==================================================================
    #  主题 / 悬浮窗
    # ==================================================================

    def _apply_tooltip_style(self, is_dark):
        from PyQt6.QtWidgets import QApplication
        if is_dark:
            qss = ("QToolTip { background-color: #333333; color: #FFFFFF; "
                   "border: 1px solid #555555; padding: 4px 8px; "
                   "border-radius: 4px; }")
        else:
            qss = ("QToolTip { background-color: #FDFBF7; color: #3A2F25; "
                   "border: 1px solid #D9D0C2; padding: 4px 8px; "
                   "border-radius: 4px; }")
        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(qss)

    def toggle_theme(self, checked):
        self.is_dark_theme = checked
        self.setStyleSheet(DARK_STYLE if checked else LIGHT_STYLE)
        self._apply_tooltip_style(checked)
        self.title_bar.apply_theme(checked)
        if self.roller_bar is not None:
            self.roller_bar.apply_theme(checked)
        self.tab_widget.tabBar().update()
        self.update()
        self.info_window.apply_theme(checked)
        for i in range(self.tab_widget.count()):
            tab = self.tab_widget.widget(i)
            if tab:
                try:
                    tab.toggle_theme(checked)
                except Exception:
                    pass

    def on_info_tab_clicked(self, index):
        if 0 <= index < self.tab_widget.count():
            self.tab_widget.setCurrentIndex(index)

    def on_info_tab_double_clicked(self, index):
        self.on_tab_double_clicked(index)

    def on_tab_double_clicked(self, index):
        if not (0 <= index < self.tab_widget.count()):
            return
        tab = self.tab_widget.widget(index)
        device = getattr(tab, 'device_serial', None)
        if not device:
            return
        self._engine.send(UICommands.ACTIVATE_WINDOW, {'device': device})

    # ==================================================================
    #  系统事件
    # ==================================================================

    def changeEvent(self, event):
        if event.type() == event.Type.WindowStateChange:
            if self.windowState() & Qt.WindowState.WindowMinimized:
                self.hide_to_tray()
                return
        super().changeEvent(event)

    def closeEvent(self, event):
        if self._quitting:
            event.accept()
            return
        event.ignore()
        self.hide_to_tray()

    def on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.show_window()

    # ==================================================================
    #  绘制 / 位置
    # ==================================================================

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if getattr(self, 'is_dark_theme', False):
            bg_color = QColor("#2B2723")
        else:
            bg_color = QColor("#F5F0E8")
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(bg_color)

        rect = self.rect()
        radius = min(14, max(0, rect.height() // 2 - 1))
        painter.drawRoundedRect(rect, radius, radius)

    def _load_window_position(self):
        pos = self._settings.data.get('window_pos')
        screen = QApplication.primaryScreen()
        if screen is None:
            return
        avail = screen.availableGeometry()

        use_saved = False
        if pos and isinstance(pos, list) and len(pos) == 2:
            x, y = pos
            if (x >= avail.left()
                    and y >= avail.top()
                    and x + self.width() <= avail.right()
                    and y + self._content_h <= avail.bottom()):
                use_saved = True

        if use_saved:
            self.move(x, y)
        else:
            self.move(avail.right() - self.width(),
                      avail.bottom() - self._content_h - 1)

    def moveEvent(self, event):
        super().moveEvent(event)
        if getattr(self, '_quitting', False):
            return
        if getattr(self, '_suppress_pos_save', False):
            return
        if hasattr(self, '_save_pos_timer'):
            self._save_pos_timer.start()

    def _do_save_pos(self):
        self._settings.data['window_pos'] = [self.x(), self.y()]
        self._settings.save()

    def _save_window_position(self):
        self._settings.data['window_pos'] = [self.x(), self.y()]
        self._settings.save()

    # ==================================================================
    #  退出
    # ==================================================================

    def quit_app(self):
        self._quitting = True

        try:
            if self._engine.is_connected():
                self._engine.send(UICommands.SHUTDOWN, {})
        except Exception:
            pass

        try:
            self._engine.disconnect()
        except Exception:
            pass

        if self.tray_icon:
            self.tray_icon.hide()
            self.tray_icon.deleteLater()
            self.tray_icon = None

        self.info_window.hide()
        QApplication.quit()