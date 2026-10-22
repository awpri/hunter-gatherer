"""Pluggable text-to-speech.

Three backends, selected by settings.tts:
  • none   – no audio (text-only mode; the default)
  • piper  – local neural TTS (piper-tts); great quality, runs offline
  • openai – OpenAI TTS cloud API (tts-1)

All backends implement `speak(text)` which plays audio out the default device
or returns audio bytes when playback isn't possible.
"""
from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

from hunter_gatherer.config import Settings

log = logging.getLogger(__name__)


@runtime_checkable
class TTS(Protocol):
    def speak(self, text: str) -> None: ...


class NoneTTS:
    """No-op (text mode)."""

    def speak(self, text: str) -> None:
        return None


class OpenAITTS:
    """Cloud TTS via OpenAI. Plays audio via sounddevice if available."""

    def __init__(self, api_key: str, voice: str = "alloy"):
        self.api_key = api_key
        self.voice = voice

    def speak(self, text: str) -> None:
        try:
            from openai import OpenAI
        except ImportError as exc:
            from hunter_gatherer.optional_dependencies import OptionalDependencyError

            raise OptionalDependencyError("OpenAI speech", "tts-openai", "openai") from exc

        client = OpenAI(api_key=self.api_key)
        resp = client.audio.speech.create(model="tts-1", voice=self.voice, input=text)
        _play_pcm(resp.content)


class PiperTTS:
    """Local neural TTS via piper-tts."""

    def __init__(self, model_path: str):
        self.model_path = model_path

    def speak(self, text: str) -> None:
        try:
            from piper import PiperVoice  # type: ignore
        except ImportError as e:
            log.warning("piper-tts not installed (%s); skipping TTS", e)
            return
        voice = PiperVoice.load(self.model_path)
        for chunk in voice.synthesize_stream_raw(text):
            _play_pcm(chunk)


def _play_pcm(audio_bytes: bytes) -> None:
    """Best-effort audio playback. Falls back to writing a wav if no device."""
    try:
        # Decode via the stdlib wave module if possible (raw PCM otherwise).
        import io
        import wave

        import numpy as np
        import sounddevice as sd  # type: ignore

        try:
            with wave.open(io.BytesIO(audio_bytes), "rb") as wf:
                data = wf.readframes(wf.getnframes())
                audio = np.frombuffer(data, dtype=np.int16)
                sr = wf.getframerate()
            sd.play(audio, sr)
            sd.wait()
            return
        except Exception:
            pass
        # Fallback: treat as raw int16 mono @ 22050 (OpenAI's default).
        audio = np.frombuffer(audio_bytes, dtype=np.int16)
        sd.play(audio, 22050)
        sd.wait()
    except Exception as e:
        log.debug("audio playback unavailable (%s); skipping", e)


def make_tts(settings: Settings) -> TTS:
    """Factory: build the configured TTS backend, or NoneTTS."""
    backend = settings.tts.lower()
    if backend == "openai" and settings.has_openai:
        return OpenAITTS(api_key=settings.openai_api_key)
    if backend == "piper":
        path = settings.extra.get("piper_model", "en_US-lessac-medium.onnx") if hasattr(settings, "extra") else "en_US-lessac-medium.onnx"
        return PiperTTS(model_path=path)
    return NoneTTS()


def speak(text: str, settings: Settings | None = None) -> None:
    """Convenience: speak `text` using the configured backend."""
    settings = settings or Settings()
    make_tts(settings).speak(text)


# ── Streaming / chunked synthesis (2026-08-11 #2) ────────────────────────────
_SENTENCE_END = (".", "!", "?", "。", "！", "？")
_CLAUSE_END = (",", ";", ":", "，", "；")


class ChunkedTTS:
    """Buffer token deltas; flush to the TTS engine at sentence/clause
    boundaries so synthesis overlaps generation (2026-08-11 #2).

    Feeding the loop's ``token`` deltas and flushing at ``. ! ?`` (or a
    ~max_chars backstop) means the first sentence starts speaking while the
    model is still generating the rest — first-audio-out drops from
    whole-turn latency to first-sentence latency. ``stop()`` drops any
    pending buffer (barge-in)."""

    def __init__(self, tts: TTS, *, max_chars: int = 180):
        self.tts = tts
        self.max_chars = max_chars
        self._buf = ""
        self._stopped = False
        self.chunks: list[str] = []  # observability (tests/transcripts)

    def feed(self, delta: str) -> None:
        """Accept a token delta; speak a chunk when a boundary arrives."""
        if self._stopped or not delta:
            return
        self._buf += delta
        if len(self._buf) >= self.max_chars or self._at_boundary():
            self.flush()

    def _at_boundary(self) -> bool:
        stripped = self._buf.rstrip()
        if not stripped:
            return False
        return stripped.endswith(_SENTENCE_END) or (
            len(self._buf) >= self.max_chars // 2 and stripped.endswith(_CLAUSE_END)
        )

    def flush(self) -> None:
        """Speak whatever is buffered (the tail of the stream)."""
        text = self._buf.strip()
        self._buf = ""
        if text:
            self.chunks.append(text)
            self.tts.speak(text)

    def stop(self) -> None:
        """Barge-in: drop the pending buffer; further feeds are ignored."""
        self._stopped = True
        self._buf = ""


class _StopPlayback(Exception):
    pass


def stop_playback() -> None:
    """Signal the active playback loop to stop (barge-in hook). Raises
    _StopPlayback, caught by the playing thread."""
    raise _StopPlayback()
