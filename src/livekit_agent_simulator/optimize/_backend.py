"""Judge-backend wiring + test seam for the optimizer's LLM proposer."""

from __future__ import annotations

from typing import Any, Protocol


class OptimizeProposer(Protocol):
    async def propose(self, *, system: str, user: str) -> str:
        ...


def proposer_for(project_root: str, backend_override: Any = None) -> OptimizeProposer:
    """Return a proposer bound to a project's judge config (or an injected stub).

    Reuses the exact judge backend path as ``evals/runner._judge`` so candidate
    generation honors the configured ``judge:`` model/base_url without a new dep.
    """
    if backend_override is not None:
        return backend_override

    async def _bound(*, system: str, user: str) -> str:
        from ..config import load_config
        from ..evals.backend import backend_from_config
        from ..evals.resolve import resolve_judge

        cfg = load_config(project_root)
        resolved = resolve_judge(cfg.judge, sim_api_key=cfg.simulator.api_key)
        if not resolved.ready:
            raise RuntimeError(resolved.skip_reason or "judge not configured")
        backend = backend_from_config(cfg.judge, cfg.simulator.api_key)
        if backend is None:
            raise RuntimeError("judge backend unavailable")
        return await backend.complete_json(system=system, user=user)

    return _bound
