"""Shared SIP error formatting for legs."""

from __future__ import annotations

from typing import Any


def sip_error_spec(exc: BaseException, *, call_to: str) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "call_to": call_to,
        "error": f"{type(exc).__name__}: {exc}",
    }
    meta = getattr(exc, "metadata", None)
    if isinstance(meta, dict):
        if meta.get("sip_status_code") is not None:
            spec["sip_status_code"] = meta.get("sip_status_code")
        if meta.get("sip_status") is not None:
            spec["sip_status"] = meta.get("sip_status")
    code = getattr(exc, "sip_status_code", None)
    if code is not None:
        spec["sip_status_code"] = code
    status = getattr(exc, "sip_status", None)
    if status is not None:
        spec["sip_status"] = status
    return spec
