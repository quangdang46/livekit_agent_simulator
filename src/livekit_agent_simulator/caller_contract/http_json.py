"""Shared JSON-over-HTTP transport for the caller-contract adapters.

Four near-identical POST loops existed before this module: the two text
backends (``OpenAITextBackend``, ``GeminiTextBackend``) and the two router
adapters (``OpenAIResponseRouter``, ``GeminiResponseRouter``). Forking a
transport is how the router ended up with a retry policy the text backend did
not have, and vice versa, with nothing recording the divergence.

**What is extracted, and what deliberately is not:**

- Extracted: urlopen, JSON encode/decode, HTTP header assembly, HTTPError /
  URLError classification, and the 500-char detail truncation that all four
  did identically.
- NOT extracted: the request *envelope*. ``OpenAITextBackend`` sends
  ``response_format: {"type": "json_object"}`` — schema-free, because the
  text backend keys are matched lexically at the parse step. The router
  sends a real runtime-generated JSON Schema under strict structured
  outputs. Merging those envelopes would make one of the two wrong, so
  each adapter keeps building its own body and this module only carries it.

The retry seam is the one genuinely open design decision. It is resolved as
follows, and the reasoning is kept here because "no retry" vs "one retry" is
load-bearing and the next person will otherwise assume it was arbitrary.

**Decision: the policy is an injected predicate, and its ABSENCE means
"never retry".** So ``retry_policy=None`` (what the text backends pass) is
``post_json``'s default behaviour, and the router adapters pass
``should_retry`` from ``router.py``. The alternative — putting no retry in
the shared function and letting each adapter wrap it — was rejected because
it re-states "no retry" at four call sites, and "no retry" stated four times
is exactly the kind of thing that drifts into "one retry" somewhere without a
test noticing. The rule itself is NOT re-derived here: ``should_retry`` stays
the single authority for 429/5xx-vs-4xx.

**Decision: the raised type is a caller-supplied factory.** The shared
function cannot know whether a 400 means "bad text-backend prompt" or "bad
router schema" — the diagnostics that matter are the caller's. So
``error_factory`` builds the message and the exception type, and this module
stays ignorant of both vocabularies.

Note this preserves current behaviour exactly. The text backends had no
retry and keep none; the router adapters had one retry on 429/5xx and keep
exactly that. Nothing is made "more consistent" than it was — the point is
that the divergence is now visible in one place instead of hidden in four.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Callable

#: Every adapter truncated the provider's error body to this much. Long enough
#: to see the provider's own message (which is the useful part — a 400 from a
#: strict schema says which field it rejected), short enough not to paste a
#: stack trace into a log line.
ERROR_DETAIL_CHARS = 500


def build_request(
    *,
    url: str,
    body: dict[str, Any],
    api_key: str,
    auth_style: str = "bearer",
) -> urllib.request.Request:
    """Assemble the request. Shared so the four adapters cannot drift on
    headers or on how a credential is attached."""
    if auth_style == "bearer":
        auth = f"Bearer {api_key}"
    elif auth_style == "raw":  # Gemini takes the key in the URL, not a header
        auth = ""
    else:
        raise ValueError(f"unknown auth_style {auth_style!r}")

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if auth:
        headers["Authorization"] = auth
    return urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"), method="POST", headers=headers
    )


def _detail(exc: urllib.error.HTTPError) -> str:
    return exc.read().decode("utf-8", errors="replace")[:ERROR_DETAIL_CHARS]


# TODO(human): design the retry seam, then implement `post_json` below.
#
# Four adapters shared "POST and decode". They did NOT share a retry policy,
# and that difference is load-bearing:
#
#   - `OpenAITextBackend` / `GeminiTextBackend`: NO retry. Any HTTPError,
#     including 429 and 503, raises immediately.
#   - `OpenAIResponseRouter` / `GeminiResponseRouter`: exactly ONE retry on
#     transport errors and 429/5xx, NEVER on any 4xx. The comment there says
#     why: a 400 against our own runtime-generated schema is a builder bug, and
#     retrying it only turns a loud failure into a slow one.
#
# So the shared function needs a way to express BOTH. What to decide:
#
#   1. How the policy is passed. Candidates: a `retries: int` plus an
#      injected `should_retry(status) -> bool` predicate; a single
#      `retry: Callable[[int], bool] | None` where None means "never"; or no
#      retry inside the shared function at all, leaving each adapter to wrap
#      it. The last is the smallest diff and the least abstraction — worth
#      weighing against the fact that "no retry" is then re-stated at four
#      call sites.
#   2. What the raised exception type is. Today the callers raise their own
#      (`LanguageBackendError`, `RouterError`) with their own message prefix.
#      Either pass a factory (`error_factory: Callable[[str], Exception]`) so
#      the message shape stays each adapter's, or let the shared function
#      raise one type and have callers translate.
# Constraints worth respecting:
#   - `should_retry` already exists in `router.py` and is the authority for
#     429/5xx vs 4xx. Reuse it; do not re-derive the rule here.
#   - The envelope MUST stay with the caller. This module must never add or
#     inspect a `response_format` / schema key.
#   - Retry must re-read the error body per attempt, or the second attempt
#     reads an already-consumed stream.
def post_json(
    *,
    request: urllib.request.Request,
    timeout_s: float,
    error_factory: Callable[[str], Exception],
    retry_policy: Callable[[int], bool] | None = None,
    max_attempts: int = 1,
) -> dict[str, Any]:
    """POST `request`, decode the JSON body, raise via `error_factory`.

    ``retry_policy(status)`` decides whether a given HTTP status is retried.
    ``None`` — the default — means NEVER retry, which is what the two text
    backends do and must keep doing. The router adapters pass
    ``should_retry`` with ``max_attempts=2``.

    Exactly one retry even when ``max_attempts`` is larger would be a
    different policy; the callers below all want 1 or 2, and an unbounded
    retry against a paid API is never the answer, so the cap is small and
    explicit rather than derived.

    The error body is read inside the loop on every attempt. An ``HTTPError``
    holds a stream that is consumed once, so a retry that reused a read
    detail would raise ``ValueError`` from ``json.loads`` instead of the
    provider's real message — the failure would look like a transport bug.
    """
    last: Exception | None = None
    for attempt in range(max(1, max_attempts)):
        try:
            with urllib.request.urlopen(request, timeout=timeout_s) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            err = error_factory(f"HTTP {exc.code}: {_detail(exc)}")
            if retry_policy is None or not retry_policy(exc.code):
                raise err from exc
            last = err
        except urllib.error.URLError as exc:
            last = error_factory(f"unreachable: {exc}")
    raise last or error_factory("unreachable")
