import os
import sys
from email.parser import Parser
from email.policy import default
from pathlib import Path
from zipfile import ZipFile

import pytest


server_root = str(Path(__file__).resolve().parent.parent)
if server_root not in sys.path:
    sys.path.insert(0, server_root)

from src.capabilities.speech import speech as speech_module
from src.capabilities.speech.speech import SpeechCapability
from src.capabilities.speech.tts_module import (
    ReferenceAudio,
    TTSModule,
    get_tts_server_key,
)
from src.capabilities.speech.tts_server import TTSServer


def test_bundled_wheel_uses_standalone_distribution_name():
    server_root = Path(__file__).resolve().parent.parent
    wheels = list(
        (server_root / "res" / "packages").glob(
            "gsv_tts_lite_multispeaker-*.whl"
        )
    )

    assert len(wheels) == 1
    assert not list(
        (server_root / "res" / "packages").glob("gsv_tts_lite-*.whl")
    )

    with ZipFile(wheels[0]) as archive:
        metadata_path = next(
            name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
        )
        metadata = Parser(policy=default).parsestr(
            archive.read(metadata_path).decode("utf-8")
        )

    assert metadata["Name"] == "gsv-tts-lite-multispeaker"

    setup = (server_root / "setup.bat").read_text(encoding="utf-8")
    assert "gsv_tts_lite_multispeaker-*.whl" in setup
    assert "pip uninstall -y gsv-tts-lite" in setup
    assert "pip install --force-reinstall" in setup


class FakeModule:
    def __init__(self, server):
        self.tts_server = server
        self.say_calls = []
        self.stream_calls = []

    async def synthesize_speech_with_tone(self, text, tone, *, speaker=None):
        self.say_calls.append((text, tone, speaker))
        return b"audio"

    def stream_synthesize_speech_with_tone(self, text, tone, *, speaker=None):
        self.stream_calls.append((text, tone, speaker))
        yield b"chunk"

    @staticmethod
    def encode_audio_to_base64(audio):
        return audio.decode("ascii")


class FakeServer:
    def __init__(self):
        self.stop_calls = 0

    def request_stop(self):
        pass

    def stop(self):
        self.stop_calls += 1


def install_fake_factory(monkeypatch):
    created_servers = []

    def init_module(_config, *, tts_server=None):
        if tts_server is None:
            tts_server = FakeServer()
            created_servers.append(tts_server)
        return FakeModule(tts_server)

    monkeypatch.setattr(speech_module, "init_tts_module", init_module)
    return created_servers


def test_server_key_normalizes_equivalent_paths(tmp_path):
    config_path = tmp_path / "tts.yaml"
    equivalent_path = tmp_path / "nested" / ".." / "tts.yaml"

    first = get_tts_server_key({"server_config_path": str(config_path)})
    second = get_tts_server_key({"server_config_path": str(equivalent_path)})

    assert first == second
    assert first[1] == os.path.normcase(os.path.realpath(config_path))


def test_server_key_accepts_standalone_backend_name(tmp_path):
    key = get_tts_server_key(
        {
            "backend": "gsv-tts-lite-multispeaker",
            "server_config_path": str(tmp_path / "tts.yaml"),
        }
    )

    assert key[0] == "gsv_tts"


def test_tts_module_rejects_unsupported_language():
    with pytest.raises(ValueError, match="Unsupported TTS language"):
        TTSModule({"language": "fr"}, tts_server=object())


@pytest.mark.asyncio
async def test_tts_module_forwards_language_to_sync_and_stream_calls():
    class RecordingServer:
        def __init__(self):
            self.sync_kwargs = None
            self.stream_kwargs = None

        def synthesize(self, *_args, **kwargs):
            self.sync_kwargs = kwargs
            return b"sync-audio"

        def stream_synthesize(self, *_args, **kwargs):
            self.stream_kwargs = kwargs
            yield b"stream-audio"

    server = RecordingServer()
    module = object.__new__(TTSModule)
    module.tts_server = server
    module.language = "ja"
    module.quiet_logs = True
    module.reference_audio = {
        "normal": ReferenceAudio("reference.wav", "prompt text")
    }

    assert await module.synthesize_speech(
        "hello",
        "normal",
        speaker="speaker-a",
    ) == b"sync-audio"
    assert list(
        module.stream_synthesize_speech(
            "hello",
            "normal",
            speaker="speaker-a",
        )
    ) == [b"stream-audio"]

    expected = {
        "speaker": "speaker-a",
        "text_language": "ja",
        "prompt_language": "ja",
    }
    assert server.sync_kwargs == expected
    assert {
        key: server.stream_kwargs[key]
        for key in expected
    } == expected


def test_tts_server_preserves_languages_in_request_messages(tmp_path):
    class AliveProcess:
        @staticmethod
        def is_alive():
            return True

    class RecordingQueue:
        def __init__(self):
            self.messages = []

        def put(self, message):
            self.messages.append(message)

    server = TTSServer(str(tmp_path / "tts.yaml"))
    queue = RecordingQueue()
    server.server_process = AliveProcess()
    server.request_queue = queue
    server.response_queue = object()
    server._wait_for_response = lambda **_kwargs: {
        "ok": True,
        "audio_bytes": b"audio",
        "is_final": True,
    }

    assert server.synthesize(
        "hello",
        "speaker.wav",
        "prompt.wav",
        "prompt text",
        speaker="speaker-a",
        text_language="en",
        prompt_language="ja",
    ) == b"audio"
    assert list(
        server.stream_synthesize(
            "hello",
            "speaker.wav",
            "prompt.wav",
            "prompt text",
            speaker="speaker-a",
            text_language="zh",
            prompt_language="en",
        )
    ) == [b"audio"]

    assert queue.messages[0]["text_language"] == "en"
    assert queue.messages[0]["prompt_language"] == "ja"
    assert queue.messages[1]["text_language"] == "zh"
    assert queue.messages[1]["prompt_language"] == "en"


def test_characters_share_worker_by_backend_config_and_flags(monkeypatch, tmp_path):
    created_servers = install_fake_factory(monkeypatch)
    config_path = tmp_path / "tts.yaml"
    speech = SpeechCapability(
        {
            "luotianyi": {
                "server_config_path": str(config_path),
                "speaker": "lty",
            },
            "yanhe": {
                "server_config_path": str(tmp_path / "nested" / ".." / "tts.yaml"),
                "speaker": "yanhe",
            },
        }
    )

    assert len(created_servers) == 1
    assert speech.tts_module["luotianyi"].tts_server is created_servers[0]
    assert speech.tts_module["yanhe"].tts_server is created_servers[0]


def test_worker_flags_are_part_of_server_ownership_key(monkeypatch, tmp_path):
    created_servers = install_fake_factory(monkeypatch)
    config_path = str(tmp_path / "tts.yaml")

    SpeechCapability(
        {
            "quiet": {
                "server_config_path": config_path,
                "suppress_worker_output": True,
            },
            "verbose": {
                "server_config_path": config_path,
                "suppress_worker_output": False,
            },
        }
    )

    assert len(created_servers) == 2


@pytest.mark.asyncio
async def test_character_speaker_mapping_reaches_say_and_stream(monkeypatch, tmp_path):
    install_fake_factory(monkeypatch)
    speech = SpeechCapability(
        {
            "luotianyi": {
                "server_config_path": str(tmp_path / "tts.yaml"),
                "speaker": "speaker-lty",
            }
        }
    )
    module = speech.tts_module["luotianyi"]

    assert await speech.say("luotianyi", "hello", "normal") == "audio"
    assert list(speech.say_stream("luotianyi", "hello", "normal")) == ["chunk"]
    assert module.say_calls == [("hello", "normal", "speaker-lty")]
    assert module.stream_calls == [("hello", "normal", "speaker-lty")]


@pytest.mark.asyncio
async def test_shared_worker_stops_once_and_stop_is_idempotent(monkeypatch, tmp_path):
    created_servers = install_fake_factory(monkeypatch)
    config_path = str(tmp_path / "tts.yaml")
    speech = SpeechCapability(
        {
            "first": {"server_config_path": config_path},
            "second": {"server_config_path": config_path},
        }
    )

    await speech.stop()
    await speech.stop()

    assert len(created_servers) == 1
    assert created_servers[0].stop_calls == 1


def test_partial_shared_module_failure_rolls_back_worker_once(monkeypatch, tmp_path):
    server = FakeServer()
    calls = 0

    def init_module(_config, *, tts_server=None):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("module failed")
        return FakeModule(tts_server or server)

    monkeypatch.setattr(speech_module, "init_tts_module", init_module)
    config_path = str(tmp_path / "tts.yaml")

    with pytest.raises(RuntimeError, match="module failed"):
        SpeechCapability(
            {
                "first": {"server_config_path": config_path},
                "second": {"server_config_path": config_path},
            }
        )

    assert server.stop_calls == 1


def test_invalid_speaker_mapping_fails_before_start(monkeypatch, tmp_path):
    created_servers = install_fake_factory(monkeypatch)

    with pytest.raises(ValueError, match="non-empty string"):
        SpeechCapability(
            {
                "luotianyi": {
                    "server_config_path": str(tmp_path / "tts.yaml"),
                    "speaker": "",
                }
            }
        )

    assert created_servers == []
