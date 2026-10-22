"""Silero VAD wrapper.

Silero VAD detects speech segments in an audio stream at <1ms per 30ms chunk
on a single CPU thread. We use it to chunk a continuous recording into
utterances so faster-whisper only transcribes speech (the biggest latency win
before STT).

Loaded lazily; the harness works without it (falls back to a single chunk).
"""
from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass

log = logging.getLogger(__name__)

# 16kHz mono, Silero's required format.
SAMPLE_RATE = 16000
CHUNK_MS = 30
CHUNK_SAMPLES = SAMPLE_RATE * CHUNK_MS // 1000  # 480 samples


@dataclass
class Utterance:
    """A detected speech segment."""
    audio: list[float]      # mono float32 samples, 16kHz
    start_ms: float
    end_ms: float


class SileroVAD:
    """Wraps the Silero VAD model (via torch hub or the silero-vad package).

    Falls back to a no-op (always-on) chunker if torch/silero aren't installed.
    """

    def __init__(self, threshold: float = 0.5, min_speech_ms: int = 250, min_silence_ms: int = 800):
        self.threshold = threshold
        self.min_speech = min_speech_ms
        self.min_silence = min_silence_ms
        self._model = None
        self._available = False

    def _load(self):
        if self._model is not None:
            return self._model
        try:
            # Prefer the silero-vad package (v5+), which exposes get_speech_timestamps.
            from silero_vad import load_silero_vad  # type: ignore

            self._model = load_silero_vad()
            self._available = True
        except Exception:
            try:
                # Fall back to torch hub.
                import torch

                model, _ = torch.hub.load(repo_or_dir="snakers4/silero-vad", model="silero_vad", trust_repo=True)
                self._model = model
                self._available = True
            except Exception as e:
                log.warning("Silero VAD unavailable (%s); using no-op chunker", e)
                self._available = False
        return self._model

    @property
    def available(self) -> bool:
        self._load()
        return self._available

    def speech_fraction(self, audio, sample_rate: int = SAMPLE_RATE) -> float:
        """Fraction of the buffer classified as speech (2026-08-14 #2 barge-in
        signal). Uses the model when available; falls back to a simple energy
        gate (RMS floor) so barge-in still works in minimal installs."""
        import numpy as np

        x = np.asarray(audio, dtype=np.float32)
        if len(x) == 0:
            return 0.0
        if self._available and self._model is not None:
            try:
                from silero_vad import get_speech_timestamps

                ts = get_speech_timestamps(x, self._model, sampling_rate=sample_rate)
                speech = sum(t["end"] - t["start"] for t in ts)
                return min(1.0, speech / len(x))
            except Exception:
                pass  # fall through to the energy gate
        rms = float(np.sqrt(np.mean(np.square(x))))
        return 1.0 if rms > 0.02 else 0.0

    def segment(self, audio, sample_rate: int = SAMPLE_RATE) -> Iterator[Utterance]:
        """Yield utterances from a mono float32 array."""
        self._load()
        if not self._available or self._model is None:
            # No-op fallback: one utterance covering the whole buffer.
            yield Utterance(audio=list(audio), start_ms=0, end_ms=1000 * len(audio) / sample_rate)
            return

        try:
            from silero_vad import get_speech_timestamps  # type: ignore

            ts = get_speech_timestamps(
                audio,
                self._model,
                threshold=self.threshold,
                min_speech_duration_ms=self.min_speech,
                min_silence_duration_ms=self.min_silence,
                return_seconds=True,
                sampling_rate=sample_rate,
            )
            for seg in ts:
                s = int(seg["start"] * sample_rate)
                e = int(seg["end"] * sample_rate)
                yield Utterance(audio=list(audio[s:e]), start_ms=seg["start"] * 1000, end_ms=seg["end"] * 1000)
        except Exception as e:  # pragma: no cover
            log.warning("VAD segmentation failed (%s); falling back to whole-buffer", e)
            yield Utterance(audio=list(audio), start_ms=0, end_ms=1000 * len(audio) / sample_rate)
