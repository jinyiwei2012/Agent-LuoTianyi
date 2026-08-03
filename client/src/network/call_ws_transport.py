""" /call_ws 专用 WebSocket 传输（与聊天 WsTransport 相互独立，避免状态互相污染）。

协议与 App 端 `app/utils/call_transport.ts` 完全一致：JSON envelope + Base64 音频，
心跳 10 秒，断线后约 2 秒重连并使用原 call_id 发送 call.resume。
"""

import asyncio
import json
import ssl
import threading
import time
import uuid
from typing import Callable

import websockets

from .event_types import WSEventType, build_event, parse_server_message
from ..utils.logger import get_logger
from ..utils.tls import create_default_ssl_context

# 通话状态（与 App 端 CallStatus 对齐）
CALL_STATUS_IDLE = "idle"
CALL_STATUS_CONNECTING = "connecting"
CALL_STATUS_REQUESTING = "requesting"
CALL_STATUS_ACTIVE = "active"
CALL_STATUS_RECONNECTING = "reconnecting"
CALL_STATUS_ENDING = "ending"
CALL_STATUS_ENDED = "ended"


class CallWsTransport:
    """电话 WebSocket 传输。

    callbacks:
        on_event(event_type: str, payload: dict): 服务端业务事件（call.audio.chunk 等）
        on_status(status: str): 通话状态变化
        on_error(message: str): 可展示的错误信息
    """

    def __init__(
        self,
        base_url: str,
        username_getter: Callable[[], str | None],
        token_getter: Callable[[], str | None],
        callbacks: dict,
        verify_ssl: bool = True,
        heartbeat_interval: float = 10.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.username_getter = username_getter
        self.token_getter = token_getter
        self.callbacks = callbacks or {}
        self.verify_ssl = verify_ssl
        self.heartbeat_interval = heartbeat_interval

        self.logger = get_logger(self.__class__.__name__)

        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ws = None
        self._stop_event = threading.Event()
        self._ready_event = threading.Event()
        self._lock = threading.Lock()
        self._submit_lock = threading.Lock()
        self._ack_waiter: dict | None = None

        self._call_id: str | None = None
        self._status = CALL_STATUS_IDLE

    # ────────────────────────────── 对外接口 ──────────────────────────────

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._ready_event.clear()
        self._thread = threading.Thread(target=self._thread_entry, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._ready_event.clear()
        self._notify_ack_failure("电话连接已停止")
        if self._loop and self._ws:
            try:
                asyncio.run_coroutine_threadsafe(self._ws.close(), self._loop)
            except Exception:
                pass

    @property
    def call_id(self) -> str | None:
        return self._call_id

    @property
    def status(self) -> str:
        return self._status

    def start_call(self, ack_timeout: float = 5.0) -> dict:
        self._call_id = None
        self._set_status(CALL_STATUS_REQUESTING)
        return self._submit_event_with_ack(
            build_event(WSEventType.CALL_START, payload={}),
            ack_timeout=ack_timeout,
        )

    def resume_call(self, call_id: str, ack_timeout: float = 5.0) -> dict:
        return self._submit_event_with_ack(
            build_event(WSEventType.CALL_RESUME, payload={"call_id": call_id}),
            ack_timeout=ack_timeout,
        )

    def append_audio(self, audio_base64: str, seq: int) -> None:
        self._send_event(
            build_event(
                WSEventType.CALL_AUDIO_APPEND,
                payload={"call_id": self._call_id, "audio": audio_base64, "seq": seq},
            )
        )

    def hangup(self, ack_timeout: float = 5.0) -> dict:
        self._set_status(CALL_STATUS_ENDING)
        return self._submit_event_with_ack(
            build_event(WSEventType.CALL_HANGUP, payload={"call_id": self._call_id}),
            ack_timeout=ack_timeout,
        )

    def playback_completed(self, audio_id: str, response_id: str | None = None) -> None:
        self._send_event(
            build_event(
                WSEventType.CALL_PLAYBACK_COMPLETED,
                payload={"call_id": self._call_id, "audio_id": audio_id, "response_id": response_id},
            )
        )

    def playback_stopped(self, audio_id: str, response_id: str | None = None) -> None:
        self._send_event(
            build_event(
                WSEventType.CALL_PLAYBACK_STOPPED,
                payload={"call_id": self._call_id, "audio_id": audio_id, "response_id": response_id},
            )
        )

    # ────────────────────────────── 内部实现 ──────────────────────────────

    def _thread_entry(self) -> None:
        asyncio.run(self._run())

    async def _run(self) -> None:
        while not self._stop_event.is_set():
            self._loop = asyncio.get_running_loop()
            ws_url = self._build_ws_url(self.base_url)
            ssl_ctx = self._build_ssl_context(self.base_url)
            try:
                async with websockets.connect(ws_url, max_size=16 * 1024 * 1024, ssl=ssl_ctx) as ws:
                    self._ws = ws
                    self._ready_event.clear()
                    await self._authenticate(ws)
                    recv_task = asyncio.create_task(self._recv_loop(ws))
                    hb_task = asyncio.create_task(self._heartbeat_loop(ws))
                    done, pending = await asyncio.wait(
                        [recv_task, hb_task],
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    for task in pending:
                        task.cancel()
                    for task in done:
                        exc = task.exception()
                        if exc:
                            self.logger.error(f"Call WebSocket inner task exited with error: {exc}")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.logger.error(f"Call WebSocket connection error: {exc}")
                self._notify_ack_failure("电话连接断开")
            finally:
                self._ws = None
                self._ready_event.clear()
                if not self._stop_event.is_set() and self._status in (
                    CALL_STATUS_REQUESTING,
                    CALL_STATUS_ACTIVE,
                    CALL_STATUS_RECONNECTING,
                ):
                    # 断线进入重连保留期；服务端最多保留 5 秒
                    self._set_status(CALL_STATUS_RECONNECTING)
                    self._emit_error("电话连接断开，正在恢复…")
                    await asyncio.sleep(2)
            # 状态已结束或主动停止时退出重连循环
            if self._stop_event.is_set() or self._status in (CALL_STATUS_ENDED, CALL_STATUS_IDLE):
                return

    async def _authenticate(self, ws) -> None:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=5)
            msg = parse_server_message(raw)
            if msg and msg.event_type == WSEventType.AUTH_OK:
                self._ready_event.set()
                return
        except Exception:
            pass

        username = self.username_getter()
        token = self.token_getter()
        if not username or not token:
            self.logger.error("Call WebSocket auth failed: missing username or token")
            return

        auth_event = build_event(WSEventType.USER_AUTH, payload={"username": username, "token": token})
        await ws.send(json.dumps(auth_event.__dict__(), ensure_ascii=False))

        for _ in range(10):
            try:
                raw = await ws.recv()
            except Exception:
                return
            msg = parse_server_message(raw)
            if not msg:
                continue
            if msg.event_type == WSEventType.AUTH_OK:
                self.logger.debug("Call WebSocket auth successful")
                self._ready_event.set()
                if self._call_id and self._status == CALL_STATUS_RECONNECTING:
                    asyncio.create_task(self._resume_after_reconnect())
                return
            if msg.event_type in (WSEventType.AUTH_ERROR, WSEventType.SERVER_ERROR):
                self.logger.error(f"Call WebSocket auth failed: {msg.payload.get('message')}")
                self._emit_error(f"电话鉴权失败：{msg.payload.get('message')}")
                return

    async def _resume_after_reconnect(self) -> None:
        if not self._call_id:
            return
        result = await asyncio.to_thread(self.resume_call, self._call_id)
        if not result.get("ok"):
            self.logger.error(f"Call resume failed: {result.get('error')}")

    async def _recv_loop(self, ws) -> None:
        while not self._stop_event.is_set():
            try:
                raw = await ws.recv()
            except websockets.ConnectionClosed:
                # 主动停止或对端关闭时的正常退出路径
                return
            msg = parse_server_message(raw)
            if not msg or not msg.event_type:
                continue
            event_type = msg.event_type.value if isinstance(msg.event_type, WSEventType) else str(msg.event_type)
            payload = msg.payload or {}

            if event_type == WSEventType.SERVER_ACK.value:
                if payload.get("call_id"):
                    self._call_id = str(payload["call_id"])
                self._complete_ack_waiter(
                    ok=payload.get("ok", True),
                    error=str(payload.get("message") or payload.get("code") or ""),
                    reply_to=msg.reply_to,
                )
                continue

            if event_type == WSEventType.CALL_REQUESTED.value:
                if payload.get("call_id"):
                    self._call_id = str(payload["call_id"])
            elif event_type in (WSEventType.CALL_CONNECTED.value, WSEventType.CALL_RESUMED.value):
                self._set_status(CALL_STATUS_ACTIVE)
            elif event_type == WSEventType.CALL_RECONNECTING.value:
                self._set_status(CALL_STATUS_RECONNECTING)
            elif event_type in (WSEventType.CALL_ENDED.value, WSEventType.CALL_REJECTED.value):
                self._call_id = None
                self._set_status(CALL_STATUS_ENDED)
            elif event_type == WSEventType.CALL_ERROR.value:
                self._emit_error(str(payload.get("message") or "通话发生错误"))

            self._emit_event(event_type, payload)

    async def _heartbeat_loop(self, ws) -> None:
        ping_id = 0
        while not self._stop_event.is_set():
            if self._ready_event.is_set():
                ping_id += 1
                hb_event = build_event(WSEventType.HB_PING, payload={"ping_id": ping_id})
                try:
                    await ws.send(json.dumps(hb_event.__dict__(), ensure_ascii=False))
                except Exception:
                    return
            await asyncio.sleep(self.heartbeat_interval)

    def _submit_event_with_ack(self, event, ack_timeout: float) -> dict:
        request_id = event.client_msg_id
        self.start()
        if not self._ready_event.wait(timeout=8):
            return {"ok": False, "request_id": request_id, "error": "电话连接尚未就绪"}
        with self._submit_lock:
            waiter = {"request_id": request_id, "event": threading.Event(), "result": None}
            with self._lock:
                self._ack_waiter = waiter
            if not self._send_event(event):
                with self._lock:
                    if self._ack_waiter is waiter:
                        self._ack_waiter = None
                return {"ok": False, "request_id": request_id, "error": "发送失败"}
            if not waiter["event"].wait(timeout=max(0.1, ack_timeout)):
                with self._lock:
                    if self._ack_waiter is waiter:
                        self._ack_waiter = None
                self.logger.error(f"Call ACK timeout for request_id {request_id} after {ack_timeout}s")
                return {"ok": False, "request_id": request_id, "error": "等待电话服务确认超时"}
            result = waiter["result"] or {"ok": False, "request_id": request_id, "error": "未知 ACK 状态"}
            with self._lock:
                if self._ack_waiter is waiter:
                    self._ack_waiter = None
            return result

    def _send_event(self, event) -> bool:
        if not self._ready_event.is_set() or not self._loop:
            return False

        async def _send() -> None:
            if not self._ws:
                return
            await self._ws.send(json.dumps(event.__dict__(), ensure_ascii=False))

        try:
            fut = asyncio.run_coroutine_threadsafe(_send(), self._loop)
            fut.result(timeout=1)
            return True
        except Exception as exc:
            self._notify_ack_failure(f"发送失败: {exc}")
            return False

    def _complete_ack_waiter(self, ok: bool, error: str, reply_to: str | None) -> bool:
        with self._lock:
            waiter = self._ack_waiter
            if not waiter:
                return False
            expected = waiter.get("request_id")
            if reply_to and expected and reply_to != expected:
                return False
            waiter["result"] = {"ok": ok, "request_id": expected, "error": error or None}
            waiter["event"].set()
            return True

    def _notify_ack_failure(self, error_text: str) -> None:
        with self._lock:
            waiter = self._ack_waiter
            if not waiter:
                return
            waiter["result"] = {"ok": False, "request_id": waiter.get("request_id"), "error": error_text}
            waiter["event"].set()

    def _set_status(self, status: str) -> None:
        self._status = status
        callback = self.callbacks.get("on_status")
        if callback:
            try:
                callback(status)
            except Exception:
                pass

    def _emit_event(self, event_type: str, payload: dict) -> None:
        callback = self.callbacks.get("on_event")
        if callback:
            try:
                callback(event_type, payload)
            except Exception:
                pass

    def _emit_error(self, message: str) -> None:
        callback = self.callbacks.get("on_error")
        if callback:
            try:
                callback(message)
            except Exception:
                pass

    @staticmethod
    def _build_ws_url(base_url: str) -> str:
        if base_url.startswith("https://"):
            return "wss://" + base_url[len("https://"):].rstrip("/") + "/call_ws"
        if base_url.startswith("http://"):
            return "ws://" + base_url[len("http://"):].rstrip("/") + "/call_ws"
        raise ValueError("base_url must start with http:// or https://")

    def _build_ssl_context(self, base_url: str):
        if not base_url.startswith("https://"):
            return None
        ctx = create_default_ssl_context()
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        return ctx
