"""Wake-word gate + barge-in (2026-08-14 #2).

Turns voice from push-to-talk into an ambient "hey harness" mode:

  • **Wake gate** — openWakeWord (optionally a custom-trained model) streams
    16kHz mono frames from the existing sounddevice input; on detection,
    control hands off to the normal VoiceSession capture path.
  • **Echo/refractory guard** — detection is suppressed while TTS is playing
    (so the harness doesn't wake on its own voice) and for a short refractory
    period after any activation or playback end.
  • **Barge-in** — while TTS plays back audio, the already-loaded Silero VAD
    runs on mic frames; user speech above threshold fires the agent's
    existing ``cancel_event`` and stops playback mid-chunk — the Stop
    mechanism built for the UI, reused for full-duplex voice.

The always-on loop becomes: wake → VAD → STT → routed loop → chunked TTS,
where the wake-word is simply the outermost gate (the "latency wins come from
gating" principle).

openWakeWord is an optional dep (``pip install openwakeword``); when missing,
``WakeWordDetector`` raises with an install hint at construction so callers
can fall back to push-to-talk.
"""
from __future__ import annotations

import contextlib
import logging
import threading
import time

log = logging.getLogger(__name__)


class WakeWordDetector:
    """openWakeWord wrapper with echo suppression + refractory period."""

    def __init__(self, *, model_name: str = "hey_jarvis", refractory_ms: int = 800,
                 threshold: float = 0.5):
        self.model_name = model_name
        self.refractory_ms = refractory_ms
        self.threshold = threshold
        self._model = None
        self._last_activation = 0.0
        self._muted = False  # echo guard: True while TTS is playing
        self._lock = threading.Lock()
        self._load()

    def _load(self):
        try:
            from openwakeword.model import Model
        except ImportError as e:
            raise RuntimeError(
                "openwakeword not installed; install with: pip install openwakeword "
                "(or keep push-to-talk mode)"
            ) from e
        self._model = Model(wakeword_models=[self.model_name], inference_framework="onnx")

    # ── echo/refractory guards ─────────────────────────────────────────────
    def mute(self) -> None:
        """Suppress detection while the harness is speaking (echo guard)."""
        with self._lock:
            self._muted = True

    def unmute(self, *, refractory: bool = True) -> None:
        """Re-enable detection after playback ends, honoring the refractory
        window so the tail of TTS can't immediately re-trigger."""
        with self._lock:
            self._muted = False
            if refractory:
                self._last_activation = time.monotonic()

    def _suppressed(self) -> bool:
        with self._lock:
            if self._muted:
                return True
            return (time.monotonic() - self._last_activation) * 1000 < self.refractory_ms

    # ── detection ──────────────────────────────────────────────────────────
    def process(self, pcm_frame: bytes) -> bool:
        """Feed one 16kHz mono 16-bit frame (1280 samples ≈ 80ms, openWakeWord's
        native chunk). Returns True exactly once per activation."""
        if self._suppressed():
            # Still consume the frame so the model's internal state stays warm.
            with contextlib.suppress(Exception):
                self._predict(pcm_frame)
            return False
        if self._predict(pcm_frame) >= self.threshold:
            self._last_activation = time.monotonic()
            return True
        return False

    def _predict(self, pcm_frame: bytes) -> float:
        import numpy as np

        x = np.frombuffer(pcm_frame, dtype=np.int16)
        scores = self._model.predict(x)
        for name, score in scores.items():
            if self.model_name in name or name == self.model_name:
                return float(score)
        return 0.0
