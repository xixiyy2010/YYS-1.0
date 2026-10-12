# ui/engine_client.py
"""
EngineClient —— UI 侧的引擎客户端

职责：
    1. 包装 infra.ui_bridge.UIClient
    2. 用 pyqtSignal 把事件回调送到 Qt 主线程（QueuedConnection 保证）
    3. 提供简洁接口：connect / send / 信号订阅

线程模型：
    UIClient 的接收线程在子线程
    → 收到事件 → emit sig_event（跨线程信号）
    → Qt 自动排队到主线程处理
    因此 UI 侧订阅 sig_event 的槽一定是主线程执行，可安全碰部件。

依赖：
    contracts.ui_protocol
    infra.ui_bridge
    PyQt6
"""
from __future__ import annotations

import logging
from typing import Optional

from PyQt6.QtCore import QObject, pyqtSignal

from contracts.ui_protocol import (
    UI_PIPE_NAME, UICommands,
)
from infra.ui_bridge import UIClient


logger = logging.getLogger(__name__)


class EngineClient(QObject):
    """
    UI 侧的引擎客户端。

    信号：
        sig_event(name: str, data: dict)   收到引擎事件
        sig_connected()                    UI 连上引擎
        sig_disconnected()                 引擎断开 / 引擎失联

    用法：
        client = EngineClient()
        client.sig_event.connect(self._on_engine_event,
                                 Qt.ConnectionType.QueuedConnection)
        client.sig_disconnected.connect(self._on_engine_lost,
                                        Qt.ConnectionType.QueuedConnection)
        client.connect_to_engine()      # 带重试
        client.send(UICommands.SYNC, {})
    """

    sig_event = pyqtSignal(str, dict)
    sig_connected = pyqtSignal()
    sig_disconnected = pyqtSignal()

    def __init__(self, parent=None, address: str = UI_PIPE_NAME):
        super().__init__(parent)
        self._address = address
        self._client: Optional[UIClient] = None
        self._connected = False

    # ==================================================================
    #  连接
    # ==================================================================

    def connect_to_engine(self, timeout: float = 3.0) -> bool:
        """
        连接引擎。失败返回 False（不抛异常）。
        成功后启动后台接收，进入"可用"状态。
        """
        if self._client is not None:
            return self._connected

        cli = UIClient(self._address)
        if not cli.connect(timeout=timeout):
            logger.warning('EngineClient: 连接引擎失败 %s', self._address)
            cli.close()
            return False

        self._client = cli
        self._connected = True

        # 启动后台接收（子线程 → emit 信号回主线程）
        cli.start_recv(self._on_raw_event)

        logger.info('EngineClient: 已连接引擎')
        self.sig_connected.emit()
        return True

    def disconnect(self) -> None:
        """主动断开（UI 关闭时调用）"""
        self._close_locked()

    # ==================================================================
    #  发送
    # ==================================================================

    def send(self, name: str, data: dict = None) -> bool:
        """发命令。未连接时返回 False。"""
        cli = self._client
        if cli is None or not self._connected:
            return False
        ok = cli.send_command(name, data or {})
        if not ok:
            # 发送失败视作失联
            self._on_lost()
        return ok

    # ==================================================================
    #  属性
    # ==================================================================

    def is_connected(self) -> bool:
        return self._connected

    # ==================================================================
    #  内部
    # ==================================================================

    def _on_raw_event(self, name: str, data: dict) -> None:
        """
        子线程回调：只 emit 信号，不碰任何 UI 部件。
        QueuedConnection 保证槽在主线程执行。
        """
        try:
            self.sig_event.emit(name, data or {})
        except Exception:
            logger.exception('EngineClient: sig_event.emit 异常 name=%s', name)

    def _on_lost(self) -> None:
        """连接断开 / 发送失败。只处理一次。"""
        if not self._connected and self._client is None:
            return
        self._close_locked()
        logger.warning('EngineClient: 引擎失联')
        self.sig_disconnected.emit()

    def _close_locked(self) -> None:
        self._connected = False
        cli = self._client
        self._client = None
        if cli is not None:
            try:
                cli.close()
            except Exception:
                pass