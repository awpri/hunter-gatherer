"""Voice I/O: Silero VAD + faster-whisper STT + pluggable TTS.

Canonical 2025 low-latency stack, four stages:
    gate (wake-word or push-to-talk) → Silero VAD → faster-whisper → agent → TTS

The single biggest latency win is gating: don't run Whisper continuously.
VAD chunks audio into utterances so Whisper only sees speech segments.
faster-whisper with int8 + beam_size=1 + VAD filter gives ~4× openai/whisper
speed at equivalent accuracy.

Streaming (2026-08-11 #2): ``VoiceSession.ask_stream`` runs the loop with
token streaming and speaks sentence chunks as they complete — first-audio-out
is first-sentence latency, not whole-turn.

Hands-free (2026-08-14 #2): ``wake.WakeWordDetector`` is the outermost gate
("hey harness" via openWakeWord, echo-suppressed while speaking), and
``VoiceSession.arm_barge_in`` lets the user interrupt playback — user speech
fires the loop's cancel_event and drops pending TTS.

Speculative prefetch (2026-08-19 #3): ``make_prefetch(loop)`` builds a
SpeculativePrefetch fed by STT partials; it warms the router's route cache
(and exact-match recall) while the user is still speaking, so route+recall
latency hides behind their own speech — closing the last documented voice
extension point (streaming STT partials) and completing the end-to-end
streaming chain.
"""
from __future__ import annotations

from hunter_gatherer.voice.prefetch import SpeculativePrefetch, make_prefetch
from hunter_gatherer.voice.stt import VoiceSession, transcribe_bytes, transcribe_file
from hunter_gatherer.voice.tts import ChunkedTTS, speak
from hunter_gatherer.voice.wake import WakeWordDetector

__all__ = [
    "transcribe_file",
    "transcribe_bytes",
    "VoiceSession",
    "speak",
    "ChunkedTTS",
    "WakeWordDetector",
    "SpeculativePrefetch",
    "make_prefetch",
]
