"""Speech-to-text via faster-whisper.

faster-whisper (CTranslate2 backend) is up to 4× faster than openai/whisper at
the same accuracy and lower memory. Recommended realtime config used here:
  • model: base (good accuracy/speed tradeoff; swap to small or large-v3)
  • compute_type: int8 (near-full accuracy, major speed gain)
  • beam_size: 1 (default; fine for streaming)
  • VAD filter: on (skips silence before transcribing)

We expose three entry points:
  • transcribe_file(path)          – one-shot from a wav/mp3/m4a file
  • transcribe_bytes(samples)      – one-shot from raw 16kHz mono samples
  • VoiceSession                   – push-to-talk loop reading from the mic
"""
from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass

from proharness.config import Settings
from proharness.voice.vad import SAMPLE_RATE, SileroVAD

log = logging.getLogger(__name__)


@dataclass
class Segment:
    text: str
    start_ms: float
    end_ms: float


class WhisperSTT:
    """faster-whisper wrapper. Loaded lazily so importing the voice module is
    cheap even when faster-whisper isn't installed."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._model = None
        self.vad = SileroVAD()

    def _load(self):
        if self._model is not None:
            return self._model
        try:
            from faster_whisper import WhisperModel

            self._model = WhisperModel(
                self.settings.whisper_model,
                device=self.settings.whisper_device,
                compute_type=self.settings.whisper_compute,
            )
        except Exception as e:
            log.error(
                "faster-whisper not available (%s). Install with: "
                "pip install 'proharness[voice]'",
                e,
            )
            raise
        return self._model

    def transcribe_samples(self, samples, sample_rate: int = SAMPLE_RATE,
                           on_partial=None) -> list[Segment]:
        """Transcribe raw 16kHz mono float32 samples, VAD-segmented.

        ``on_partial`` (2026-08-19 #3) receives the accumulated transcript
        after each completed utterance — the streaming-partial hook the
        speculative prefetch feeds on (multi-utterance turns surface each
        finished phrase as it lands instead of only at the end)."""
        model = self._load()
        out: list[Segment] = []
        acc: list[str] = []
        # Use VAD to chunk into utterances; skip silence.
        for utt in self.vad.segment(samples, sample_rate):
            if not utt.audio:
                continue
            try:
                import numpy as np

                audio = np.asarray(utt.audio, dtype=np.float32)
            except ImportError:
                audio = utt.audio  # type: ignore
            segments, _info = model.transcribe(
                audio,
                beam_size=1,
                vad_filter=True,
                language=None,  # auto-detect
            )
            for s in segments:
                out.append(Segment(text=s.text.strip(), start_ms=utt.start_ms + s.start * 1000, end_ms=utt.start_ms + s.end * 1000))
                acc.append(s.text.strip())
            if on_partial is not None and acc:
                with contextlib.suppress(Exception):
                    on_partial(" ".join(acc))
        return out

    def transcribe_file(self, path: str) -> list[Segment]:
        """Transcribe an audio file (wav/mp3/m4a/flac). faster-whisper handles
        decoding via ffmpeg/av. VAD filter applies inside."""
        model = self._load()
        segments, _info = model.transcribe(path, beam_size=1, vad_filter=True, language=None)
        return [Segment(text=s.text.strip(), start_ms=s.start * 1000, end_ms=s.end * 1000) for s in segments]


# ── module-level convenience ──────────────────────────────────────────────────
_model_cache: dict[int, WhisperSTT] = {}


def _get(settings: Settings) -> WhisperSTT:
    key = id(settings)
    if key not in _model_cache:
        _model_cache[key] = WhisperSTT(settings)
    return _model_cache[key]


def transcribe_file(path: str, settings: Settings | None = None) -> str:
    """One-shot: transcribe an audio file and return the joined text."""
    settings = settings or Settings()
    segs = _get(settings).transcribe_file(path)
    return " ".join(s.text for s in segs).strip()


def transcribe_bytes(samples, settings: Settings | None = None, on_partial=None) -> str:
    """One-shot: transcribe raw 16kHz mono float32 samples."""
    settings = settings or Settings()
    segs = _get(settings).transcribe_samples(samples, on_partial=on_partial)
    return " ".join(s.text for s in segs).strip()


class VoiceSession:
    """Push-to-talk mic loop. Reads audio while `recording` is true, then VAD-
    segments and transcribes on release.

    Usage:
        with VoiceSession(settings) as vs:
            vs.start()           # begin recording
            ...                  # user speaks
            vs.stop()            # stop and transcribe
            text = vs.last_text
    """

    def __init__(self, settings: Settings, *, silence_seconds: float | None = None):
        self.settings = settings
        self.stt = _get(settings)
        self.silence_seconds = silence_seconds  # if set, auto-stop after N s of silence
        self._frames: list = []
        self._stream = None
        self._recording = False
        self.last_text: str = ""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.stop()
        return False

    def start(self, on_partial=None):
        """Begin capturing from the default microphone.

        ``on_partial`` (2026-08-19 #3): a throttled monitor thread decodes the
        live buffer (~1/s) and calls ``on_partial(text)`` as the hypothesis
        stabilizes — feed it a SpeculativePrefetch so route/recall warm up
        while the user is still speaking."""
        try:
            import sounddevice as sd  # type: ignore
        except ImportError as e:
            raise RuntimeError(
                "sounddevice not installed; install with: pip install 'proharness[voice]'"
            ) from e

        self._frames = []
        self._recording = True

        def callback(indata, frames, time_info, status):  # noqa: ARG001
            if self._recording:
                self._frames.append(indata.copy())

        self._stream = sd.InputStream(
            samplerate=SAMPLE_RATE, channels=1, dtype="float32", callback=callback, blocksize=1024
        )
        self._stream.start()
        log.info("recording…")
        if on_partial is not None:
            self._start_partial_monitor(on_partial)

    def _start_partial_monitor(self, on_partial, *, interval: float = 1.0):
        """Background thread: decode the buffered-so-far audio every ~1s and
        emit the hypothesis when it changes. Failure-tolerant — partials are
        best-effort latency hiding, never a correctness path."""
        import threading

        self._partial_stop = threading.Event()
        last_emitted = ""

        def _monitor():
            nonlocal last_emitted
            while not self._partial_stop.is_set() and self._recording:
                self._partial_stop.wait(interval)
                if self._partial_stop.is_set() or not self._recording or not self._frames:
                    continue
                try:
                    import numpy as np

                    audio = np.concatenate(self._frames).flatten()
                    if len(audio) < SAMPLE_RATE // 2:  # need ≥0.5s of audio
                        continue
                    text = " ".join(
                        s.text for s in self.stt.transcribe_samples(audio)
                    ).strip()
                    if text and text != last_emitted:
                        last_emitted = text
                        with contextlib.suppress(Exception):
                            on_partial(text)
                except Exception as e:
                    log.debug("partial decode failed: %s", e)

        threading.Thread(target=_monitor, daemon=True).start()

    def stop_partial_monitor(self) -> None:
        m = getattr(self, "_partial_stop", None)
        if m is not None:
            m.set()

    def stop(self) -> str:
        """Stop capturing and transcribe. Returns the recognized text."""
        if not self._recording:
            return self.last_text
        self._recording = False
        self.stop_partial_monitor()
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None
        if not self._frames:
            self.last_text = ""
            return ""
        try:
            import numpy as np

            audio = np.concatenate(self._frames).flatten()
        except ImportError:
            # Flatten without numpy (slower).
            audio = [s for frame in self._frames for row in frame for s in row]  # type: ignore
        self.last_text = " ".join(s.text for s in self.stt.transcribe_samples(audio)).strip()
        return self.last_text

    # ── streaming answer playback (2026-08-11 #2) ──────────────────────────
    def ask_stream(self, text: str, loop, tts=None, *, on_event=None, prefetch=None) -> str:
        """Run one voice turn with early-start TTS: the loop streams token
        deltas while a worker generates, and the ChunkedTTS speaks each
        sentence as it completes — first-audio-out becomes first-sentence
        latency instead of whole-turn.

        ``prefetch`` (2026-08-19 #3) is an optional SpeculativePrefetch whose
        matched entry is handed to loop.run so the route (and exact-match
        recall) computed while the user was speaking is reused.

        Returns the full answer text. Barge-in (2026-08-14 #2): if the caller
        cancels via the returned handle's ``cancel()``, generation stops and
        pending speech is dropped."""
        import queue
        import threading

        from proharness.voice.tts import ChunkedTTS, make_tts

        tts = tts or make_tts(self.settings)
        chunked = ChunkedTTS(tts)
        cancel_event = threading.Event()
        q: queue.Queue = queue.Queue()
        precomputed = prefetch.match(text) if prefetch is not None else None

        def _relay(ev):
            kind = getattr(ev, "kind", "")
            if kind == "token":
                q.put(("delta", ev.data.get("delta", "")))
            elif kind == "turn_end":
                q.put(("end", ev.data.get("answer", "")))
            elif kind == "error":
                q.put(("end", ""))

        worker = threading.Thread(
            target=lambda: (
                loop.run(text, on_event=_relay, stream_final=True, cancel_event=cancel_event,
                         precomputed=precomputed),
                q.put(("end", None)),  # ensure termination even without turn_end
            ),
            daemon=True,
        )
        worker.start()
        answer = ""
        while True:
            try:
                kind, payload = q.get(timeout=120)
            except queue.Empty:
                break
            if kind == "delta":
                chunked.feed(payload)
            else:  # end
                if payload:
                    answer = payload
                break
        chunked.flush()  # tail synthesis of any remainder
        worker.join(timeout=5.0)
        self.last_answer = answer
        return answer

    # ── barge-in (2026-08-14 #2) ────────────────────────────────────────────
    def arm_barge_in(self, cancel_event, tts=None, *, check_interval: float = 0.1):
        """Watch the mic while TTS plays; user speech fires ``cancel_event``
        (stopping generation) and stops the chunked playback. Returns the
        watcher thread (daemon)."""
        import threading

        from proharness.voice.vad import SileroVAD

        vad = SileroVAD()

        def _watch():
            try:
                import sounddevice as sd
            except ImportError:
                return
            frames: list = []

            def callback(indata, frames_n, time_info, status):  # noqa: ARG001
                frames.append(indata.copy())

            with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                                callback=callback, blocksize=1024):
                while not cancel_event.is_set():
                    threading.Event().wait(check_interval)
                    if not frames:
                        continue
                    import numpy as np

                    audio = np.concatenate(frames[-8:]).flatten()  # last ~0.5s
                    if len(audio) and vad.speech_fraction(audio) > 0.6:
                        cancel_event.set()  # stop generation…
                        if tts is not None:
                            tts.stop()  # …and drop pending speech
                        break

        t = threading.Thread(target=_watch, daemon=True)
        t.start()
        return t
