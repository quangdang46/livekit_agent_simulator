"""Audio degradation effects for the simulated caller's mic.

Applies packet-loss / echo / phone-band / static degradation to the caller PCM
so the agent under test is stressed with the imperfect audio real callers hit —
the "call your users actually make" gap LangWatch Scenario exposes in its
``effects`` pipeline (noise / prosody / quality). lks keeps the surface small:
knobs under ``Persona.speech_conditions.effects``, each a pure per-frame PCM
transform, deterministic, no new runtime deps.

Effects are applied AFTER mixing (speech + noise → one frame) in
``ParallelMicMixer._pop_frame``, so the recorder captures exactly what the
agent would hear. They run on PCM16 mono at the caller output rate (24 kHz).
"""

from __future__ import annotations

import array
import random
from typing import Any, Callable

# A post-mix frame transform: PCM16 mono bytes in, PCM16 mono bytes out.
EffectFn = Callable[[bytes], bytes]


def _seeded_rng(*parts: Any) -> random.Random:
    """Build a deterministic RNG from the effect's config.

    The same effect config (e.g. ``packet_loss probability=0.05 chunk_ms=20``)
    always yields the same dropout pattern across runs, so a scenario with
    degradation is reproducible in CI. Different configs seed differently.
    """
    seed = "|".join(str(p) for p in parts)
    return random.Random(seed)


def _pcm_to_samples(pcm: bytes) -> array.array:
    if not pcm:
        return array.array("h")
    if len(pcm) % 2:
        pcm = pcm[:-1]
    a = array.array("h")
    a.frombytes(pcm)
    return a


def _samples_to_pcm(samples: array.array) -> bytes:
    return samples.tobytes()


def _dropout(samples: array.array, chunk_samples: int, probability: float, rng: random.Random) -> array.array:
    """Zero out random ``chunk_samples`` windows at ``probability``.

    ``chunk_samples`` is clamped to the frame length so short mixer frames
    (e.g. 10ms @ 24 kHz = 240 samples vs a 20ms/480-sample window) still
    experience dropouts instead of silently passing through.
    """
    if probability <= 0 or not samples:
        return samples
    chunk = max(1, min(chunk_samples, len(samples)))
    out = array.array("h", samples)
    i = 0
    while i < len(out):
        if rng.random() < probability:
            out[i : i + chunk] = array.array("h", [0] * min(chunk, len(out) - i))
        i += chunk
    return out


def packet_loss(probability: float = 0.05, chunk_ms: int = 20, sample_rate: int = 24_000) -> EffectFn:
    """Simulate network dropouts: zero random ~20ms windows (seeded, reproducible)."""
    chunk = max(1, (sample_rate * chunk_ms) // 1000)
    rng = _seeded_rng("packet_loss", probability, chunk)

    def _apply(pcm: bytes) -> bytes:
        if probability <= 0 or not pcm:
            return pcm
        return _samples_to_pcm(_dropout(_pcm_to_samples(pcm), chunk, probability, rng))

    return _apply


def breaking_up(probability: float = 0.2, sample_rate: int = 24_000) -> EffectFn:
    """Intermittent connection: heavier dropouts than packet_loss (LangWatch parity, seeded)."""
    chunk = (sample_rate * 100) // 1000  # 100ms windows
    rng = _seeded_rng("breaking_up", probability, chunk)

    def _apply(pcm: bytes) -> bytes:
        if not pcm:
            return pcm
        return _samples_to_pcm(_dropout(_pcm_to_samples(pcm), chunk, probability, rng))

    return _apply


def echo(delay_ms: int = 200, decay: float = 0.5, sample_rate: int = 24_000) -> EffectFn:
    """Overlay a delayed, attenuated copy of the signal (acoustic echo)."""
    delay_samples = (sample_rate * delay_ms) // 1000
    if delay_samples <= 0:
        raise ValueError(f"echo delay_ms must be positive (got {delay_ms})")

    def _apply(pcm: bytes) -> bytes:
        if not pcm:
            return pcm
        samples = _pcm_to_samples(pcm)
        out = array.array("h", samples)
        for i, s in enumerate(samples):
            src = i - delay_samples
            if src >= 0:
                v = int(out[i]) + int(round(samples[src] * decay))
                if v > 32767:
                    v = 32767
                elif v < -32768:
                    v = -32768
                out[i] = v
        return _samples_to_pcm(out)

    return _apply


def phone_quality(sample_rate: int = 24_000) -> EffectFn:
    """Mimic a phone line: 4 kHz bandpass + mild amplitude compression.

    Simple two-pass lowpass (moving average) approximates the 300–3.4 kHz
    bandpass without heavy DSP deps. Compression gently attenuates loud peaks.
    """
    # ~4 kHz cutoff with a short moving-average window at 24 kHz → phone-ish.
    window = max(1, sample_rate // 6000)
    if sample_rate // 6000 < 1:
        window = 1

    def _apply(pcm: bytes) -> bytes:
        if not pcm:
            return pcm
        samples = _pcm_to_samples(pcm)
        out = array.array("h", [0] * len(samples))
        acc = 0
        for i, s in enumerate(samples):
            acc += int(s)
            if i >= window:
                acc -= int(samples[i - window])
            avg = acc // window
            # mild compression toward the mid-band
            v = avg
            if v > 8000:
                v = 8000 + (v - 8000) // 2
            elif v < -8000:
                v = -8000 - (v + 8000) // 2
            out[i] = v
        return _samples_to_pcm(out)

    return _apply


def static(intensity: float = 0.05) -> EffectFn:
    """Overlay white-noise static at the given intensity (fraction of full scale, seeded)."""
    if not 0.0 <= intensity <= 1.0:
        raise ValueError(f"static intensity must be in [0.0, 1.0] (got {intensity})")
    rng = _seeded_rng("static", intensity)

    def _apply(pcm: bytes) -> bytes:
        if not pcm:
            return pcm
        samples = _pcm_to_samples(pcm)
        out = array.array("h", samples)
        for i in range(len(out)):
            noise = int(round(rng.gauss(0, 32767 * intensity)))
            v = out[i] + noise
            if v > 32767:
                v = 32767
            elif v < -32768:
                v = -32768
            out[i] = v
        return _samples_to_pcm(out)

    return _apply


# name → factory
_EFFECTS: dict[str, Callable[[dict[str, Any]], EffectFn]] = {
    "packet_loss": lambda kw: packet_loss(
        probability=float(kw.get("probability", 0.05)),
        chunk_ms=int(kw.get("chunk_ms", 20)),
    ),
    "breaking_up": lambda kw: breaking_up(probability=float(kw.get("probability", 0.2))),
    "echo": lambda kw: echo(
        delay_ms=int(kw.get("delay_ms", 200)),
        decay=float(kw.get("decay", 0.5)),
    ),
    "phone_quality": lambda kw: phone_quality(),
    "static": lambda kw: static(intensity=float(kw.get("intensity", 0.05))),
}

SUPPORTED_EFFECTS = tuple(sorted(_EFFECTS))


def effects_spec_from_persona(persona: dict[str, Any] | None) -> dict[str, Any] | None:
    """Extract ``speech_conditions.effects`` from a persona dict ('' aliases included)."""
    if not isinstance(persona, dict):
        return None
    sc = persona.get("speech_conditions") or persona.get("speechConditions") or {}
    if not isinstance(sc, dict):
        return None
    return sc.get("effects")


def resolve_audio_effects(persona: dict[str, Any] | None) -> list[EffectFn]:
    """Build the ordered effect chain from ``Persona.speech_conditions.effects``.

    Accepts the persona dict (matches ``resolve_voice_gain``) and reads
    ``speech_conditions.effects``, which is either a dict mapping
    ``{name: kwargs-or-bool}`` or a list of names. Unsupported names raise so
    a typo fails fast at validate time, not mid-run.
    """
    spec = effects_spec_from_persona(persona)
    if not spec:
        return []
    if isinstance(spec, (list, tuple)):
        items = {str(n): {} for n in spec}
    elif isinstance(spec, dict):
        items = spec
    else:
        raise ValueError(
            "Persona.speech_conditions.effects must be a list or dict "
            f"(got {type(spec).__name__})"
        )

    chain: list[EffectFn] = []
    for name, cfg in items.items():
        key = str(name).strip().lower()
        if key not in _EFFECTS:
            raise ValueError(
                f"Unknown audio effect {name!r}; supported: {', '.join(SUPPORTED_EFFECTS)}"
            )
        kwargs: dict[str, Any] = {}
        if isinstance(cfg, dict):
            kwargs = cfg
        elif cfg not in (None, True):
            raise ValueError(f"audio effect {name!r} config must be a dict, true, or absent")
        chain.append(_EFFECTS[key](kwargs))
    return chain
