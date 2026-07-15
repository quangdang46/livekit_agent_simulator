"""Portable audio cue catalog: built-in package cues + per-target overrides.

Resolution (first hit wins) for a scenario ``asset`` string:

1. Absolute filesystem path
2. Config ``cues.aliases`` (name → path/asset, then re-resolve without alias loop)
3. Explicit builtin prefix: ``builtin:id`` / ``@id``
4. Scenario directory (next to the ``.jsonl``)
5. Target library: ``.agent-sim/cues/``  (**overrides** same-named built-in files)
6. Extra dirs from ``cues.dirs`` (relative to project root or absolute)
7. Package ``templates/cues/`` built-ins

Multi-repo pattern: keep defaults in the package; drop a same-named WAV under
``.agent-sim/cues/`` or set ``cues.aliases`` in that target's config.yaml.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from ..paths import package_cues_dir, package_templates_dir

CueKind = Literal["noise", "voice", "legacy"]


@dataclass(frozen=True)
class BuiltinCue:
    """One built-in short id under ``templates/cues/``."""

    file: str
    description: str
    kind: CueKind = "voice"
    # Hamming-style interrupt class when relevant (noise / correction / backchannel / escalate).
    interrupt_class: str | None = None
    locale: str | None = None
    # Expected spoken content for vocal cues (STT / assert SoT). None for noise beds.
    text: str | None = None


def _c(
    file: str,
    description: str,
    *,
    kind: CueKind = "voice",
    interrupt_class: str | None = None,
    locale: str | None = None,
    text: str | None = None,
) -> BuiltinCue:
    return BuiltinCue(
        file=file,
        description=description,
        kind=kind,
        interrupt_class=interrupt_class,
        locale=locale,
        text=text,
    )


# Short IDs → package cue metadata (file under templates/cues/)
BUILTIN_CUES: dict[str, BuiltinCue] = {
    "noise.ambient": _c(
        "ambient_noise_bed.wav",
        "Soft ambient noise bed (false interrupt / background).",
        kind="noise",
        interrupt_class="noise",
    ),
    "noise.loud": _c(
        "loud_noise_burst.wav",
        "Short loud noise burst (false interrupt).",
        kind="noise",
        interrupt_class="noise",
    ),
    "noise.blip": _c(
        "loud_interrupt_blip.wav",
        "Very short cut-in blip before/with a barge.",
        kind="noise",
        interrupt_class="noise",
    ),
    "noise.interrupt": _c(
        "loud_interrupt_blip.wav",
        "Alias of noise.blip — short cut-in blip.",
        kind="noise",
        interrupt_class="noise",
    ),
    # Legacy JA samples (filename-stable refs)
    "backchannel": _c(
        "backchannel_ja.wav",
        "Legacy JA backchannel sample.",
        kind="legacy",
        interrupt_class="backchannel",
        locale="ja-JP",
    ),
    "backchannel_ja": _c(
        "backchannel_ja.wav",
        "Legacy JA backchannel sample.",
        kind="legacy",
        interrupt_class="backchannel",
        locale="ja-JP",
    ),
    "interrupt": _c(
        "real_interrupt_ja.wav",
        "Legacy JA real-interrupt speech sample.",
        kind="legacy",
        interrupt_class="correction",
        locale="ja-JP",
    ),
    "real_interrupt_ja": _c(
        "real_interrupt_ja.wav",
        "Legacy JA real-interrupt speech sample.",
        kind="legacy",
        interrupt_class="correction",
        locale="ja-JP",
    ),
    "ambiguous": _c(
        "ambiguous_ja.wav",
        "Legacy JA ambiguous / edge false-interrupt sample.",
        kind="legacy",
        interrupt_class="noise",
        locale="ja-JP",
    ),
    "ambiguous_ja": _c(
        "ambiguous_ja.wav",
        "Legacy JA ambiguous / edge false-interrupt sample.",
        kind="legacy",
        interrupt_class="noise",
        locale="ja-JP",
    ),
    # Portable vocal speech (EN default + VI); override in .agent-sim/cues/
    "voice.backchannel": _c(
        "backchannel_uhhuh_en.wav",
        "EN backchannel sustain (uh-huh ×5, ~4s).",
        interrupt_class="backchannel",
        locale="en-US",
        text="uh-huh",
    ),
    "voice.uhhuh": _c(
        "backchannel_uhhuh_en.wav",
        "Alias of voice.backchannel — EN uh-huh sustain.",
        interrupt_class="backchannel",
        locale="en-US",
        text="uh-huh",
    ),
    "voice.backchannel_en": _c(
        "backchannel_uhhuh_en.wav",
        "Alias of voice.backchannel — EN uh-huh sustain.",
        interrupt_class="backchannel",
        locale="en-US",
        text="uh-huh",
    ),
    "voice.backchannel_yeah": _c(
        "backchannel_yeah_en.wav",
        "EN backchannel: Yeah / Okay / Mhm.",
        interrupt_class="backchannel",
        locale="en-US",
        text="Yeah. Okay. Mhm.",
    ),
    "voice.yeah": _c(
        "backchannel_yeah_en.wav",
        "Alias of voice.backchannel_yeah.",
        interrupt_class="backchannel",
        locale="en-US",
        text="Yeah. Okay. Mhm.",
    ),
    "voice.barge_short": _c(
        "barge_wait_en.wav",
        "EN short barge: “Wait a second…”.",
        interrupt_class="correction",
        locale="en-US",
        text="Wait a second…",
    ),
    "voice.barge_wait": _c(
        "barge_wait_en.wav",
        "Alias of voice.barge_short — “Wait a second…”.",
        interrupt_class="correction",
        locale="en-US",
        text="Wait a second…",
    ),
    "voice.interrupt": _c(
        "barge_wait_en.wav",
        "Alias of voice.barge_short — “Wait a second…”.",
        interrupt_class="correction",
        locale="en-US",
        text="Wait a second…",
    ),
    "voice.barge_wait_en": _c(
        "barge_wait_en.wav",
        "Alias of voice.barge_short — “Wait a second…”.",
        interrupt_class="correction",
        locale="en-US",
        text="Wait a second…",
    ),
    "voice.barge_sorry": _c(
        "barge_sorry_en.wav",
        "EN barge: “Sorry — one second…”.",
        interrupt_class="correction",
        locale="en-US",
        text="Sorry — one second…",
    ),
    "voice.barge_sorry_en": _c(
        "barge_sorry_en.wav",
        "Alias of voice.barge_sorry.",
        interrupt_class="correction",
        locale="en-US",
        text="Sorry — one second…",
    ),
    "voice.correction": _c(
        "barge_correction_en.wav",
        "EN correction barge: “No wait - I meant next Friday.”",
        interrupt_class="correction",
        locale="en-US",
        text="No wait - I meant next Friday.",
    ),
    "voice.barge_correction": _c(
        "barge_correction_en.wav",
        "Alias of voice.correction.",
        interrupt_class="correction",
        locale="en-US",
        text="No wait - I meant next Friday.",
    ),
    "voice.escalate": _c(
        "barge_escalate_en.wav",
        "EN escalate barge: ask for a human agent.",
        interrupt_class="escalate",
        locale="en-US",
        text="Stop. I need to speak with a human.",
    ),
    "voice.barge_escalate": _c(
        "barge_escalate_en.wav",
        "Alias of voice.escalate.",
        interrupt_class="escalate",
        locale="en-US",
        text="Stop. I need to speak with a human.",
    ),
    "voice.human": _c(
        "barge_escalate_en.wav",
        "Alias of voice.escalate — human handoff ask.",
        interrupt_class="escalate",
        locale="en-US",
        text="Stop. I need to speak with a human.",
    ),
    "voice.soft": _c(
        "barge_soft_en.wav",
        "EN soft barge: “Um, hang on…”.",
        interrupt_class="correction",
        locale="en-US",
        text="Um, hang on…",
    ),
    "voice.barge_soft": _c(
        "barge_soft_en.wav",
        "Alias of voice.soft.",
        interrupt_class="correction",
        locale="en-US",
        text="Um, hang on…",
    ),
    "voice.barge_vi": _c(
        "barge_wait_vi.wav",
        "VI short barge (wait / cut-in).",
        interrupt_class="correction",
        locale="vi-VN",
    ),
    "voice.barge_wait_vi": _c(
        "barge_wait_vi.wav",
        "Alias of voice.barge_vi.",
        interrupt_class="correction",
        locale="vi-VN",
    ),
    "voice.barge_long_vi": _c(
        "barge_long_vi.wav",
        "VI longer stacked barge (stresses VAD / recovery).",
        interrupt_class="correction",
        locale="vi-VN",
    ),
    "voice.backchannel_vi": _c(
        "backchannel_vi.wav",
        "VI backchannel sustain.",
        interrupt_class="backchannel",
        locale="vi-VN",
    ),
    "voice.uhhuh_vi": _c(
        "backchannel_vi.wav",
        "Alias of voice.backchannel_vi.",
        interrupt_class="backchannel",
        locale="vi-VN",
    ),
    "voice.backchannel_ja": _c(
        "backchannel_ja.wav",
        "JA backchannel (same file as legacy backchannel_ja).",
        kind="legacy",
        interrupt_class="backchannel",
        locale="ja-JP",
    ),
    "voice.interrupt_ja": _c(
        "real_interrupt_ja.wav",
        "JA interrupt speech (same file as legacy real_interrupt_ja).",
        kind="legacy",
        interrupt_class="correction",
        locale="ja-JP",
    ),
}

# Backward-compatible id → filename map (resolution + tests).
BUILTIN_ALIASES: dict[str, str] = {k: v.file for k, v in BUILTIN_CUES.items()}


def builtin_cue_meta(alias: str) -> BuiltinCue | None:
    key = alias.strip()
    if key.startswith("builtin:"):
        key = key[len("builtin:") :].strip()
    elif key.startswith("@"):
        key = key[1:].strip()
    return BUILTIN_CUES.get(key) or BUILTIN_CUES.get(key.replace("-", "_"))


@dataclass
class CueResolveContext:
    project_root: Path | None = None
    scenario_dir: Path | None = None
    cues_config: Any = None  # config.CuesConfig: .dirs, .aliases
    package_cues: Path | None = None
    def target_cues_dir(self) -> Path | None:
        if self.project_root is None:
            return None
        return self.project_root / ".agent-sim" / "cues"

    def package_dir(self) -> Path:
        if self.package_cues is not None:
            return self.package_cues
        try:
            return package_cues_dir()
        except Exception:
            return package_templates_dir() / "cues"


def _strip_prefix(asset: str) -> tuple[str, bool]:
    """Return (name, explicit_builtin)."""
    raw = asset.strip()
    if raw.startswith("builtin:"):
        return raw[len("builtin:") :].strip(), True
    if raw.startswith("@"):
        return raw[1:].strip(), True
    return raw, False


def _extra_dirs(ctx: CueResolveContext) -> list[Path]:
    out: list[Path] = []
    cfg = ctx.cues_config
    if not cfg:
        return out
    root = ctx.project_root
    for d in getattr(cfg, "dirs", None) or []:
        p = Path(d)
        if not p.is_absolute() and root is not None:
            p = (root / p).resolve()
        else:
            p = p.resolve()
        if p.is_dir():
            out.append(p)
    return out


def resolve_cue_asset(
    asset: str,
    *,
    scenario_dir: Path | None = None,
    package_root: Path | None = None,
    templates_dir: Path | None = None,
    project_root: Path | None = None,
    cues_config: Any = None,
    _alias_depth: int = 0,
) -> Path:
    """Resolve a WAV cue for room_pcm. See module docstring for search order."""
    if not asset or not str(asset).strip():
        raise FileNotFoundError("Cue asset is empty")

    ctx = CueResolveContext(
        project_root=Path(project_root).resolve() if project_root else None,
        scenario_dir=Path(scenario_dir).resolve() if scenario_dir else None,
        cues_config=cues_config,
        package_cues=(templates_dir / "cues").resolve()
        if templates_dir is not None
        else (
            (Path(package_root) / "templates" / "cues").resolve()
            if package_root is not None
            else None
        ),
    )

    name, explicit_builtin = _strip_prefix(str(asset))
    tried: list[str] = []

    # 1) Absolute path
    abs_cand = Path(name)
    if abs_cand.is_absolute():
        if abs_cand.is_file():
            return abs_cand
        tried.append(str(abs_cand))
        raise FileNotFoundError(
            f"Cue asset not found: {asset} (absolute path missing). Tried: {tried}"
        )

    # 2) Config aliases (target dynamic names)
    aliases = getattr(ctx.cues_config, "aliases", None) if ctx.cues_config else None
    if _alias_depth < 4 and isinstance(aliases, dict) and name in aliases:
        mapped = aliases[name]
        return resolve_cue_asset(
            mapped,
            scenario_dir=scenario_dir,
            package_root=package_root,
            templates_dir=templates_dir,
            project_root=project_root,
            cues_config=cues_config,
            _alias_depth=_alias_depth + 1,
        )

    # Builtin short id → filename
    builtin_file = BUILTIN_ALIASES.get(name) or BUILTIN_ALIASES.get(name.replace("-", "_"))
    if explicit_builtin:
        fname = builtin_file or name
        path = ctx.package_dir() / fname
        if path.is_file():
            return path
        tried.append(str(path))
        raise FileNotFoundError(
            f"Built-in cue not found: {asset}. Tried: {tried}. "
            f"List with: lk-sim cues"
        )

    candidates: list[Path] = []

    # 3) Scenario directory
    if ctx.scenario_dir is not None:
        candidates.append(ctx.scenario_dir / name)

    # 4) Target .agent-sim/cues/ (overrides package same name)
    tdir = ctx.target_cues_dir()
    if tdir is not None:
        candidates.append(tdir / name)
        if builtin_file:
            candidates.append(tdir / builtin_file)

    # 5) Extra dirs
    for d in _extra_dirs(ctx):
        candidates.append(d / name)
        if builtin_file:
            candidates.append(d / builtin_file)

    # 6) Package built-ins (file name or alias)
    pkg = ctx.package_dir()
    candidates.append(pkg / name)
    if builtin_file:
        candidates.append(pkg / builtin_file)

    for path in candidates:
        tried.append(str(path))
        if path.is_file():
            return path

    raise FileNotFoundError(
        f"Cue asset not found: {asset}. Tried: {tried}. "
        f"Put WAV under .agent-sim/cues/, next to the scenario, or use builtin:noise.loud. "
        f"See lk-sim cues --root <target>"
    )


def _cue_list_fields(meta: BuiltinCue | None) -> dict[str, Any]:
    if meta is None:
        return {}
    return {
        "description": meta.description,
        "kind": meta.kind,
        "interrupt_class": meta.interrupt_class,
        "locale": meta.locale,
        "text": meta.text,
    }


def list_package_cues(package_cues: Path | None = None) -> list[dict[str, Any]]:
    root = package_cues or package_cues_dir()
    items: list[dict[str, Any]] = []
    if not root.is_dir():
        return items
    files = {p.name: p for p in root.glob("*.wav")}
    # Prefer listing by alias when available
    seen_files: set[str] = set()
    for alias, meta in sorted(BUILTIN_CUES.items()):
        p = files.get(meta.file)
        if p is None:
            continue
        seen_files.add(meta.file)
        items.append(
            {
                "id": alias,
                "file": meta.file,
                "source": "builtin",
                "path": str(p),
                "ref": f"builtin:{alias}",
                **_cue_list_fields(meta),
            }
        )
    for fname, p in sorted(files.items()):
        if fname in seen_files:
            continue
        items.append(
            {
                "id": fname,
                "file": fname,
                "source": "builtin",
                "path": str(p),
                "ref": f"builtin:{fname}",
                "description": None,
                "kind": None,
                "interrupt_class": None,
                "locale": None,
                "text": None,
            }
        )
    return items


def list_target_cues(project_root: Path) -> list[dict[str, Any]]:
    root = Path(project_root).resolve() / ".agent-sim" / "cues"
    items: list[dict[str, Any]] = []
    if not root.is_dir():
        return items
    for p in sorted(root.glob("*.wav")):
        items.append(
            {
                "id": p.name,
                "file": p.name,
                "source": "target",
                "path": str(p),
                "ref": p.name,
                "overrides_builtin": (package_cues_dir() / p.name).is_file()
                if package_cues_dir().is_dir()
                else False,
            }
        )
    return items


def list_all_cues(
    project_root: Path | None = None,
    *,
    cues_config: Any = None,
) -> dict[str, Any]:
    """Catalog for CLI/MCP: builtin + target + aliases + resolve order."""
    builtin = list_package_cues()
    target: list[dict[str, Any]] = []
    aliases: dict[str, str] = {}
    extra: list[str] = []
    if project_root is not None:
        target = list_target_cues(project_root)
        if cues_config is not None:
            aliases = dict(getattr(cues_config, "aliases", {}) or {})
            root = Path(project_root).resolve()
            for d in getattr(cues_config, "dirs", None) or []:
                p = Path(d)
                if not p.is_absolute():
                    p = root / p
                extra.append(str(p))

    return {
        "resolve_order": [
            "absolute path",
            "cues.aliases (config.yaml)",
            "builtin:id / @id",
            "scenario directory",
            ".agent-sim/cues/ (target override)",
            "cues.dirs (config.yaml)",
            "package templates/cues/",
        ],
        "builtin": builtin,
        "target": target,
        "aliases": aliases,
        "extra_dirs": extra,
        "usage": {
            "scenario_asset_examples": [
                "builtin:voice.barge_short",
                "builtin:voice.backchannel",
                "builtin:noise.loud",
                "@noise.ambient",
                "loud_noise_burst.wav",
                "my_cafe.wav  # place in .agent-sim/cues/",
                "office  # if cues.aliases.office is set",
            ],
            "wav_format": "PCM16 mono @ 24000 Hz",
            "vocal_aliases": [
                "voice.barge_short",
                "voice.barge_sorry",
                "voice.backchannel",
                "voice.barge_vi",
            ],
        },
    }


def describe_resolution(
    asset: str,
    *,
    project_root: Path | None = None,
    scenario_dir: Path | None = None,
    cues_config: Any = None,
) -> dict[str, Any]:
    meta = builtin_cue_meta(asset)
    try:
        path = resolve_cue_asset(
            asset,
            project_root=project_root,
            scenario_dir=scenario_dir,
            cues_config=cues_config,
        )
        out: dict[str, Any] = {"asset": asset, "ok": True, "path": str(path)}
        if meta is not None:
            out.update(_cue_list_fields(meta))
            out["file"] = meta.file
        return out
    except FileNotFoundError as e:
        out = {"asset": asset, "ok": False, "error": str(e)}
        if meta is not None:
            out.update(_cue_list_fields(meta))
            out["file"] = meta.file
        return out
