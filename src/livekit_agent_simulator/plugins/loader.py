"""Load verify plugins from entry-points and `.agent-sim/plugins/*.py`."""

from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path
from typing import Any

from ..config import DOT_FOLDER
from . import registry

ENTRY_POINT_GROUP = "lks.plugins"


def plugins_dir(project_root: Path) -> Path:
    return project_root / DOT_FOLDER / "plugins"


def ensure_plugins_loaded(project_root: Path | str, module_names: list[str] | None = None) -> dict[str, Any]:
    """Load entry-point plugins once, then optional local modules from the target repo."""
    project_root = Path(project_root).resolve()
    loaded: list[str] = []
    errors: list[str] = []

    if registry.mark_loaded(f"entrypoints:{project_root}"):
        for err in _load_entry_points():
            errors.append(err)
        else:
            loaded.append("entrypoints:lks.plugins")

    for name in module_names or []:
        key = f"local:{project_root}:{name}"
        if not registry.mark_loaded(key):
            continue
        try:
            _load_local_module(project_root, name)
            loaded.append(f"local:{name}")
        except Exception as e:
            errors.append(f"{name}: {type(e).__name__}: {e}")

    registry.run_setup_hooks()
    return {"loaded": loaded, "errors": errors, "verify_plugins": registry.list_verify_plugins()}


def _load_entry_points() -> list[str]:
    errors: list[str] = []
    try:
        from importlib.metadata import entry_points
    except ImportError:
        return errors

    eps = entry_points()
    group = eps.select(group=ENTRY_POINT_GROUP) if hasattr(eps, "select") else eps.get(ENTRY_POINT_GROUP, [])
    for ep in group:
        try:
            fn = ep.load()
            if callable(fn):
                fn()
            else:
                errors.append(f"{ep.name}: entry point is not callable")
        except Exception as e:
            errors.append(f"{ep.name}: {type(e).__name__}: {e}")
    return errors


def _load_local_module(project_root: Path, module_name: str) -> None:
    safe = module_name.strip().replace("-", "_")
    if not safe or safe.startswith("_") or "/" in safe or "\\" in safe:
        raise ValueError(f"invalid plugin module name: {module_name!r}")

    path = plugins_dir(project_root) / f"{safe}.py"
    if not path.exists():
        raise FileNotFoundError(f"{path} not found")

    qualname = f"lks_plugin_{safe}_{abs(hash(path)) & 0xFFFFFF:x}"
    spec = importlib.util.spec_from_file_location(qualname, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[qualname] = module
    spec.loader.exec_module(module)
    setup = getattr(module, "setup", None)
    if callable(setup):
        setup()
