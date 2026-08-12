"""RMS onset detection for the agent audio channel (perceived speech onset).

Black-box perceived latency needs "when the caller actually starts hearing the
agent", not "when the agent transcript final arrives". The agent PCM that lands
in the recorder R channel is the closest black-box proxy for what the caller
hears, so we detect the first sustained rise in RMS energy above a threshold.

Design (locked with the latency spec):

- **Stateful, not a threshold gate.** SILENCE → SPEECH requires ``energy_frames``
  consecutive energy frames at/above ``threshold``; SPEECH → SILENCE requires
  ``exit_frames`` consecutive low frames. A ``refractory_ms`` guard suppresses
  re-firing on continuous speech across chunk boundaries.
- **Onset timestamp is backdated to the first frame of the onset window**, never
  the detection time. A 20ms window that needs 3 consecutive energy frames only
  confirms speech ~40-60ms after it started; emitting at detection time would
  add that detection latency onto the metric. We return the onset *frame index*,
  and the caller maps it to ``ts_mono_ms`` via the audio timeline
  (``audio_t0 + onset_frame / sample_rate``).
- **Chunk-boundary invariant.** ``push()`` receives PCM chunks of arbitrary
  length (LiveKit frame boundaries are unrelated to window boundaries). The
  detector keeps a rolling window and must produce the same onsets regardless
  of how the same sample stream is chunked.
- **Pure + no new deps** (mirrors ``degradation.py``: stdlib only).

Threshold note: 0.012 of full-scale PCM16 is the initial default for tuning,
NOT an immutable ground truth. Validate against real recordings before locking
any CI gate (see ``docs/…`` latency spec).
"""

from __future__ import annotations

import array
import math
from typing import Callable

# PCM16 full-scale for RMS normalization.
_FULL_SCALE = 32768.0


def pcm16_mono_rms(pcm: bytes) -> float:
    """Normalized RMS of PCM16 mono bytes in [0.0, 1.0] (0.0 for empty)."""
    if not pcm:
        return 0.0
    data = pcm
    if len(data) % 2:
        data = data[:-1]
    samples = array.array("h")
    samples.frombytes(data)
    if not samples:
        return 0.0
    n = len(samples)
    acc = 0.0
    for s in samples:
        acc += float(s) * float(s)
    return math.sqrt(acc / n) / _FULL_SCALE


def onset_to_audio_ms(onset_frame_idx: int, sample_rate: int) -> int:
    """Map an onset frame index to an audio-relative millisecond offset.

    ``onset_frame_idx`` is relative to the detector's sample stream t0 (the
    recorder R-channel t0). Callers align it to the run timeline with the
    existing audio ``t0_mono_ms``:

        ts_mono_ms = audio_t0_mono_ms + onset_to_audio_ms(idx, rate)
    """
    if sample_rate <= 0:
        raise ValueError(f"sample_rate must be positive (got {sample_rate})")
    return int(round(onset_frame_idx * 1000 / sample_rate))


class RmsOnsetDetector:
    """Stateful RMS onset detector returning onset *frame indices*.

    Config (all tunable, defaults from the spec):
        sample_rate      Hz of the PCM stream fed in.
        win_ms           window length in ms (onset resolution).
        threshold        normalized RMS (0.0-1.0) that counts as energy.
        energy_frames    consecutive energy windows required to leave SILENCE.
        exit_frames      consecutive quiet windows required to return to SILENCE.
        refractory_ms    min gap between two onsets (suppress chunk re-fire).

    The detector is window-agnostic to chunk boundaries: samples are appended
    to a rolling buffer and windows are consumed only when fully available.
    """

    def __init__(
        self,
        *,
        sample_rate: int,
        win_ms: int = 20,
        threshold: float = 0.012,
        energy_frames: int = 3,
        exit_frames: int = 5,
        refractory_ms: int = 60,
        on_onset: Callable[[int], None] | None = None,
    ) -> None:
        if sample_rate <= 0:
            raise ValueError(f"sample_rate must be positive (got {sample_rate})")
        if win_ms <= 0:
            raise ValueError(f"win_ms must be positive (got {win_ms})")
        if not 0.0 < threshold < 1.0:
            raise ValueError(f"threshold must be in (0.0, 1.0) (got {threshold})")
        if energy_frames < 1:
            raise ValueError(f"energy_frames must be >= 1 (got {energy_frames})")
        if exit_frames < 1:
            raise ValueError(f"exit_frames must be >= 1 (got {exit_frames})")
        if refractory_ms < 0:
            raise ValueError(f"refractory_ms must be >= 0 (got {refractory_ms})")
        self.sample_rate = sample_rate
        self.win_ms = win_ms
        self.win_samples = max(1, (sample_rate * win_ms) // 1000)
        self.threshold = float(threshold)
        self.energy_frames = int(energy_frames)
        self.exit_frames = int(exit_frames)
        self.refractory_samples = max(0, (sample_rate * refractory_ms) // 1000)
        self.on_onset = on_onset

        # Rolling mono sample buffer (window-agnostic to chunk boundaries).
        self._buf: array.array = array.array("h")
        # Consumed sample count (for frame-index mapping / dedupe windows).
        self._consumed = 0
        # State machine.
        self._speech = False  # True = in SPEECH
        self._energy_run = 0  # consecutive energy windows while SILENCE
        self._quiet_run = 0  # consecutive quiet windows while SPEECH
        self._last_onset_consumed: int | None = None  # dedupe anchor
        self._frames_before_first = 0
        self._first_onset_seen = False

    # ------------------------------------------------------------------ push

    def push(self, pcm: bytes) -> None:
        """Feed one PCM16 mono chunk (arbitrary length, any boundary)."""
        if not pcm:
            return
        data = pcm
        if len(data) % 2:
            data = data[:-1]
        samples = array.array("h")
        samples.frombytes(data)
        if not samples:
            return
        self._buf.extend(samples)
        # Consume complete windows, leaving the tail buffered for the next chunk.
        while len(self._buf) >= self.win_samples:
            win = self._buf[: self.win_samples]
            del self._buf[: self.win_samples]
            self._process_window(win)

    def push_processed(self, samples: array.array) -> None:
        """Feed already-decoded samples (internal/test convenience)."""
        if not samples:
            return
        self._buf.extend(samples)
        while len(self._buf) >= self.win_samples:
            win = self._buf[: self.win_samples]
            del self._buf[: self.win_samples]
            self._process_window(win)

    def flush(self) -> None:
        """Drop any buffered partial window (no onset can begin mid-window)."""
        del self._buf[:]

    # ------------------------------------------------------------ processing

    def _process_window(self, win: array.array) -> None:
        start = self._consumed
        self._consumed += self.win_samples
        energy = self._window_energy(win)

        if not self._speech:
            # SILENCE: accumulate consecutive energy windows.
            if energy >= self.threshold:
                self._energy_run += 1
                if self._energy_run >= self.energy_frames:
                    # Enter SPEECH. Onset begins at the FIRST frame of the run.
                    onset_start = start - (self.energy_frames - 1) * self.win_samples
                    onset_start = max(0, onset_start)
                    if not self._first_onset_seen:
                        self._frames_before_first = onset_start
                        self._first_onset_seen = True
                    if self._refractory_ok(onset_start):
                        self._speech = True
                        self._quiet_run = 0
                        self._last_onset_consumed = onset_start
                        if self.on_onset is not None:
                            self.on_onset(onset_start)
                    else:
                        # Within refractory of a prior onset — do not re-fire,
                        # but keep counting toward SPEECH so a single long burst
                        # is one onset (not many).
                        self._speech = True
                        self._quiet_run = 0
            else:
                self._energy_run = 0
        else:
            # SPEECH: exit after `exit_frames` consecutive quiet windows.
            if energy < self.threshold:
                self._quiet_run += 1
                if self._quiet_run >= self.exit_frames:
                    self._speech = False
                    self._energy_run = 0
                    self._quiet_run = 0
            else:
                self._quiet_run = 0

    def _window_energy(self, win: array.array) -> float:
        n = len(win)
        if n == 0:
            return 0.0
        acc = 0.0
        for s in win:
            acc += float(s) * float(s)
        return math.sqrt(acc / n) / _FULL_SCALE

    def _refractory_ok(self, onset_start: int) -> bool:
        if self._last_onset_consumed is None:
            return True
        return (onset_start - self._last_onset_consumed) >= self.refractory_samples

    # ---------------------------------------------------------------- state

    @property
    def speech(self) -> bool:
        """True while the detector believes speech is ongoing."""
        return self._speech

    @property
    def frames_before_first_onset(self) -> int:
        """Frames consumed before the first onset's first frame (debug)."""
        return self._frames_before_first

    @property
    def first_onset_seen(self) -> bool:
        return self._first_onset_seen
