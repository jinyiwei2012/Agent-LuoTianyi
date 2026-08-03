"""电话窗口：全屏 Live2D、通话状态、计时与挂断按钮。

复用 Live2DContainer 渲染模型；通过独立代理 Binder 设置表情，避免覆盖主窗口
Live2D 的模型绑定。协议与 App 端一致（/call_ws，JSON + Base64）。

线程模型：网络传输与音频线程的回调一律通过 Qt Signal（queued connection）
回到 GUI 线程再操作控件，避免跨线程直接访问 Qt 对象。
"""

from datetime import datetime
from typing import Any, Dict

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..message_process.call_audio_controller import CallAudioController
from ..network.call_ws_transport import (
    CALL_STATUS_ACTIVE,
    CALL_STATUS_ENDED,
    CALL_STATUS_REQUESTING,
    CALL_STATUS_RECONNECTING,
    CallWsTransport,
)
from ..network.event_types import WSEventType
from ..utils.logger import get_logger
from .main_ui import Live2DContainer


class _CallBinder(QWidget):
    """电话窗口专用的轻量 Binder 替身：只提供 Live2D 所需的表情信号。

    不调用真实 Binder 的 on_set_model，避免覆盖主窗口的模型绑定。
    """

    expression_signal = Signal(str)
    agent_thinking_signal = Signal(bool)

    def __init__(self):
        super().__init__()
        self._expression = None

    def on_set_model(self, model) -> None:
        pass

    def emit_expression_signal(self, expression: str) -> None:
        if expression != self._expression:
            self._expression = expression
            self.expression_signal.emit(expression)


class CallDialog(QDialog):
    """独立电话窗口。关闭窗口即挂断。"""

    chat_blocked = Signal(bool)  # 通话期间禁用聊天输入
    _status_signal = Signal(str)  # 传输线程 → GUI 线程
    _event_signal = Signal(str, dict)
    _error_signal = Signal(str)
    _capture_error_signal = Signal(str)

    def __init__(
        self,
        network_client,
        gui_config: Dict[str, Any],
        live2d_config: Dict[str, Any],
        agent_binder=None,
        parent=None,
    ):
        super().__init__(parent)
        self.logger = get_logger(self.__class__.__name__)
        self.network_client = network_client
        self.gui_config = gui_config
        self.live2d_config = live2d_config

        self.setWindowTitle("语音通话")
        self.resize(900, 700)
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.Window)

        self._binder = _CallBinder()
        self._transport: CallWsTransport | None = None
        self._controller = CallAudioController(
            on_mic_chunk=self._on_mic_chunk,
            on_playback_completed=self._on_playback_completed,
            on_playback_stopped=self._on_playback_stopped,
            on_capture_error=self._capture_error_signal.emit,
        )
        self._connected_at: datetime | None = None
        self._hanging_up = False
        self._closing = False

        self._init_ui()
        self._setup_timer()
        self.chat_blocked.emit(True)

        # 跨线程回调桥接
        self._status_signal.connect(self._handle_status)
        self._event_signal.connect(self._handle_server_event)
        self._error_signal.connect(self._handle_error)
        self._capture_error_signal.connect(self._handle_error)

    # ────────────────────────────── UI ──────────────────────────────

    def _init_ui(self) -> None:
        layout = QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # 全屏 Live2D
        container_config = self.gui_config.get("live2d_container", {})
        self.live2d_container = Live2DContainer(
            container_config, self.live2d_config, agent_binder=self._binder, parent=self
        )
        layout.addWidget(self.live2d_container, 1)

        # 状态栏
        status_bar = QWidget()
        status_layout = QHBoxLayout(status_bar)
        status_layout.setContentsMargins(16, 8, 16, 8)
        self.status_label = QLabel("正在请求通话…")
        self.status_label.setStyleSheet("font-size: 14px; color: #333333;")
        self.timer_label = QLabel("00:00")
        self.timer_label.setStyleSheet("font-size: 14px; color: #666666;")
        status_layout.addWidget(self.status_label)
        status_layout.addStretch()
        status_layout.addWidget(self.timer_label)
        layout.addWidget(status_bar)

        # 挂断按钮（红色圆形）
        self.hangup_btn = QPushButton("挂断")
        self.hangup_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.hangup_btn.setFixedSize(88, 88)
        self.hangup_btn.setStyleSheet(
            "QPushButton { background-color: #E5484D; color: white; font-size: 16px;"
            " border-radius: 44px; border: none; }"
            "QPushButton:hover { background-color: #D13B40; }"
            "QPushButton:pressed { background-color: #B92F34; }"
        )
        self.hangup_btn.clicked.connect(self._on_hangup_clicked)
        layout.addWidget(self.hangup_btn, 0, Qt.AlignmentFlag.AlignHCenter)

        self.setLayout(layout)

    def _setup_timer(self) -> None:
        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self._update_elapsed)

    def _update_elapsed(self) -> None:
        if self._connected_at is None:
            return
        elapsed = max(0, int((datetime.now() - self._connected_at).total_seconds()))
        minutes, seconds = divmod(elapsed, 60)
        self.timer_label.setText(f"{minutes:02d}:{seconds:02d}")

    # ────────────────────────────── 生命周期 ──────────────────────────────

    def start_call(self) -> None:
        self._transport = CallWsTransport(
            self.network_client.base_url,
            username_getter=lambda: self.network_client.user_id,
            token_getter=lambda: self.network_client.message_token,
            callbacks={
                "on_event": self._event_signal.emit,
                "on_status": self._status_signal.emit,
                "on_error": self._error_signal.emit,
            },
            verify_ssl=self.network_client.verify_ssl,
        )
        self._transport.start()
        result = self._transport.start_call()
        if not result.get("ok"):
            self._show_error_and_close(str(result.get("error") or "电话建立失败"))

    # 以下回调运行在音频线程（无 Qt 操作，只做线程安全的网络发送）
    def _on_mic_chunk(self, audio_base64: str, seq: int) -> None:
        if self._transport:
            self._transport.append_audio(audio_base64, seq)

    def _on_playback_completed(self, audio_id: str, response_id: str | None) -> None:
        if self._transport:
            self._transport.playback_completed(audio_id, response_id)

    def _on_playback_stopped(self, audio_id: str, response_id: str | None) -> None:
        if self._transport:
            self._transport.playback_stopped(audio_id, response_id)

    # 以下回调运行在 GUI 线程（经 Signal 桥接）
    def _handle_server_event(self, event_type: str, payload: dict) -> None:
        if event_type == WSEventType.CALL_AUDIO_CHUNK.value:
            audio_id = str(payload.get("audio_id") or "")
            response_id = str(payload.get("response_id") or "") or None
            audio = str(payload.get("audio") or "")
            is_final = bool(payload.get("is_final"))
            expression = str(payload.get("expression") or "")
            if expression:
                self._binder.emit_expression_signal(expression)
            if audio_id:
                self._controller.enqueue_audio(audio_id, response_id, audio, is_final)
        elif event_type == WSEventType.CALL_STOP_PLAYBACK.value:
            audio_ids = payload.get("audio_ids") or []
            self._controller.stop_playback(list(audio_ids) if isinstance(audio_ids, list) else None)
        elif event_type == WSEventType.CALL_ENDED.value:
            self._close_after_ended()
        elif event_type == WSEventType.CALL_REJECTED.value:
            message = str(payload.get("message") or payload.get("code") or "电话请求被拒绝")
            self._show_error_and_close(message)

    def _handle_status(self, status: str) -> None:
        if status == CALL_STATUS_REQUESTING:
            self.status_label.setText("正在请求通话…")
        elif status == CALL_STATUS_ACTIVE:
            self.status_label.setText("通话中")
            self._connected_at = self._connected_at or datetime.now()
            # 开始采集并发送（seq 从 0 开始，重连恢复后延续）
            self._controller.reset_seq()
            self._controller.start_capture()
            self.timer.start()
        elif status == CALL_STATUS_RECONNECTING:
            self.status_label.setText("正在恢复通话…")
            self._controller.stop_capture()
        elif status == CALL_STATUS_ENDED:
            self._close_after_ended()

    def _handle_error(self, message: str) -> None:
        if not self._closing:
            self.status_label.setText(message)

    def _on_hangup_clicked(self) -> None:
        if self._hanging_up:
            return
        self._hanging_up = True
        self.status_label.setText("正在挂断…")
        self._controller.stop_capture()
        self._controller.stop_playback()
        if self._transport:
            self._transport.hangup()
        # 服务端 call.ended 会触发关闭；若超时未收到，则本地兜底关闭
        QTimer.singleShot(6000, self._close_after_ended)

    def _show_error_and_close(self, message: str) -> None:
        if self._closing:
            return
        self._closing = True
        self.status_label.setText(message)
        QMessageBox.warning(self, "通话失败", message)
        QTimer.singleShot(200, self.close)

    def _close_after_ended(self) -> None:
        if self._closing:
            return
        self._closing = True
        QTimer.singleShot(300, self.close)

    def closeEvent(self, event) -> None:
        if not self._hanging_up and self._transport and self._transport.status != CALL_STATUS_ENDED:
            # 直接关闭窗口视为挂断
            self._controller.stop_capture()
            self._controller.stop_playback()
            self._transport.hangup()
        self._controller.close()
        if self._transport:
            self._transport.stop()
            self._transport = None
        self.chat_blocked.emit(False)
        super().closeEvent(event)
