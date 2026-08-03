"""电话音频控制器：麦克风 PCM 采集与逐句播放、停止 ACK。

- 采集：PyAudio 16-bit / 16 kHz / 单声道 PCM，2048 字节一帧，Base64 后交给传输层。
- 播放：按 audio_id 缓冲分片，收到 is_final 后整句入队；播放线程严格按句序播放，
  完整播放后回调 completed；收到停止命令时中断当前句并回调 stopped。
"""

import base64
import queue
import threading

from ..utils.audio_processor import AudioPlayerStream
from ..utils.logger import get_logger

MIC_SAMPLE_RATE = 16000
MIC_CHANNELS = 1
MIC_SAMPLE_WIDTH = 2  # 16-bit
CHUNK_BYTES = 2048  # 每帧字节数（1024 个 PCM 采样）


class CallAudioController:
    def __init__(
        self,
        on_mic_chunk=None,
        on_playback_completed=None,
        on_playback_stopped=None,
        on_capture_error=None,
    ):
        self._on_mic_chunk = on_mic_chunk
        self._on_playback_completed = on_playback_completed
        self._on_playback_stopped = on_playback_stopped
        self._on_capture_error = on_capture_error
        self.logger = get_logger(self.__class__.__name__)

        self._capture_stop = threading.Event()
        self._capture_thread: threading.Thread | None = None
        self._capture_stream = None
        self._mic_seq = 0

        self._playback_stop = threading.Event()
        self._playback_thread: threading.Thread | None = None
        self._playback_queue: queue.Queue = queue.Queue()
        self._buffers: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._current_player: AudioPlayerStream | None = None
        self._current_audio_id: str | None = None
        self._aborted_audio_ids: set[str] = set()

    # ────────────────────────────── 采集 ──────────────────────────────

    def reset_seq(self) -> None:
        """新通话开始时将采集序号归零（重连期间不调用，保证 seq 延续递增）。"""
        self._mic_seq = 0

    def start_capture(self) -> bool:
        if self._capture_thread and self._capture_thread.is_alive():
            return True
        self._capture_stop.clear()
        self._capture_thread = threading.Thread(target=self._capture_loop, daemon=True)
        self._capture_thread.start()
        return True

    def stop_capture(self) -> None:
        self._capture_stop.set()
        # 中断阻塞中的 read
        with self._lock:
            stream = self._capture_stream
        if stream is not None:
            try:
                stream.stop_stream()
                stream.close()
            except Exception:
                pass
        if self._capture_thread:
            self._capture_thread.join(timeout=2)
            self._capture_thread = None

    def _capture_loop(self) -> None:
        try:
            import pyaudio
        except ImportError:
            self._emit_capture_error("未安装 PyAudio，无法采集麦克风")
            return
        frames_per_buffer = CHUNK_BYTES // MIC_SAMPLE_WIDTH
        try:
            p = pyaudio.PyAudio()
            stream = p.open(
                format=pyaudio.paInt16,
                channels=MIC_CHANNELS,
                rate=MIC_SAMPLE_RATE,
                input=True,
                frames_per_buffer=frames_per_buffer,
            )
        except Exception as exc:
            self.logger.error(f"麦克风打开失败: {exc}")
            self._emit_capture_error(f"麦克风打开失败：{exc}")
            return
        with self._lock:
            self._capture_stream = stream
        try:
            while not self._capture_stop.is_set():
                try:
                    data = stream.read(frames_per_buffer, exception_on_overflow=False)
                except Exception:
                    break
                if self._capture_stop.is_set() or not data:
                    continue
                seq = self._mic_seq
                self._mic_seq += 1
                if self._on_mic_chunk:
                    try:
                        self._on_mic_chunk(base64.b64encode(data).decode("ascii"), seq)
                    except Exception:
                        pass
        finally:
            with self._lock:
                self._capture_stream = None
            try:
                stream.stop_stream()
                stream.close()
            except Exception:
                pass
            try:
                p.terminate()
            except Exception:
                pass

    def _emit_capture_error(self, message: str) -> None:
        if self._on_capture_error:
            try:
                self._on_capture_error(message)
            except Exception:
                pass

    # ────────────────────────────── 播放 ──────────────────────────────

    def enqueue_audio(self, audio_id: str, response_id: str, audio_base64: str, is_final: bool) -> None:
        with self._lock:
            entry = self._buffers.get(audio_id)
            if entry is None:
                entry = {"response_id": response_id, "data": bytearray()}
                self._buffers[audio_id] = entry
            if audio_base64:
                try:
                    entry["data"] += base64.b64decode(audio_base64)
                except Exception:
                    self.logger.warning(f"音频解码失败: audio_id={audio_id}")
            if is_final:
                self._buffers.pop(audio_id, None)
                data = bytes(entry["data"])
                self._playback_queue.put((audio_id, response_id, data))
        self._start_playback_thread_if_needed()

    def stop_playback(self, audio_ids: list[str] | None = None) -> None:
        """停止指定（或全部）音频播放：清空缓冲与队列，并回调 stopped ACK。"""
        buffered_stopped: list[tuple[str, str]] = []
        with self._lock:
            if audio_ids:
                for audio_id in audio_ids:
                    entry = self._buffers.pop(audio_id, None)
                    if entry is not None:
                        buffered_stopped.append((audio_id, entry["response_id"]))
            else:
                buffered_stopped.extend(
                    (audio_id, entry["response_id"]) for audio_id, entry in self._buffers.items()
                )
                self._buffers.clear()
            player = self._current_player
            current_id = self._current_audio_id
            if current_id is not None:
                self._aborted_audio_ids.add(current_id)
        if player is not None:
            # 中断阻塞中的 PyAudio write；播放线程捕获异常后按 stopped 处理
            try:
                player.close()
            except Exception:
                pass
        discarded: list[tuple[str, str]] = []
        while True:
            try:
                item = self._playback_queue.get_nowait()
            except queue.Empty:
                break
            discarded.append(item)
        for audio_id, response_id in buffered_stopped:
            self._emit_playback_stopped(audio_id, response_id)
        for audio_id, response_id, _data in discarded:
            self._emit_playback_stopped(audio_id, response_id)

    def _start_playback_thread_if_needed(self) -> None:
        if self._playback_thread and self._playback_thread.is_alive():
            return
        self._playback_stop.clear()
        self._playback_thread = threading.Thread(target=self._playback_loop, daemon=True)
        self._playback_thread.start()

    def _playback_loop(self) -> None:
        while not self._playback_stop.is_set():
            try:
                audio_id, response_id, data = self._playback_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            if self._playback_stop.is_set():
                break
            if not data:
                # 空终止包：直接视为播放完成
                self._emit_playback_completed(audio_id, response_id)
                continue
            player = AudioPlayerStream()
            with self._lock:
                self._current_player = player
                self._current_audio_id = audio_id
            aborted = False
            try:
                player.append_buffer(data)
                if not self._playback_stop.is_set():
                    player.wait_until_empty()
            except Exception:
                aborted = True
            finally:
                with self._lock:
                    self._current_player = None
                    self._current_audio_id = None
                    aborted = aborted or audio_id in self._aborted_audio_ids
                    self._aborted_audio_ids.discard(audio_id)
                try:
                    player.close()
                except Exception:
                    pass
            if aborted:
                self._emit_playback_stopped(audio_id, response_id)
            else:
                self._emit_playback_completed(audio_id, response_id)

    def _emit_playback_completed(self, audio_id: str, response_id: str) -> None:
        if self._on_playback_completed:
            try:
                self._on_playback_completed(audio_id, response_id)
            except Exception:
                pass

    def _emit_playback_stopped(self, audio_id: str, response_id: str) -> None:
        if self._on_playback_stopped:
            try:
                self._on_playback_stopped(audio_id, response_id)
            except Exception:
                pass

    def close(self) -> None:
        self.stop_capture()
        self.stop_playback()
        self._playback_stop.set()
        if self._playback_thread:
            self._playback_thread.join(timeout=2)
            self._playback_thread = None
