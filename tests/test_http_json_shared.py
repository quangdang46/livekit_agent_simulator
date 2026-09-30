"""The transport is shared; the envelopes are NOT.

Two properties, each a real regression risk:

1. **One transport.** Four adapters used to carry their own copy of the
   urlopen/decode loop. That is how the router ended up with a retry policy
   the text backend did not have, with nothing recording the divergence. These
   tests assert by MODULE IDENTITY that all four resolve to
   `http_json.post_json`, so the transport cannot be forked again silently.

2. **Separate envelopes.** The extraction deliberately stopped short of the
   request body. `OpenAITextBackend` sends
   `response_format: {"type": "json_object"}` — schema-free, because its
   response keys are matched lexically in `_parse_backend_response`. The
   router sends a real runtime-generated JSON Schema under strict structured
   outputs. If these ever merge, one of the two is wrong: either the text
   backend starts paying for schema compilation it does not need, or the
   router loses the strict contract that makes an out-of-set id
   structurally impossible.

Property 2 is the one that would be easy to "fix" by accident during a future
cleanup, so it is asserted on the actual request bodies, not on source text.
"""

from __future__ import annotations

import inspect
import json
from unittest.mock import patch

import pytest

from livekit_agent_simulator.caller_contract import http_json
from livekit_agent_simulator.caller_contract.router_gemini import GeminiResponseRouter
from livekit_agent_simulator.caller_contract.router_openai import OpenAIResponseRouter
from livekit_agent_simulator.caller_contract.text_backends import (
    GeminiTextBackend,
    OpenAITextBackend,
    _build_user_prompt,
)
from livekit_agent_simulator.caller_contract.responses import ResponseCatalog
from livekit_agent_simulator.caller_contract.router import build_route_schema


def _catalog() -> ResponseCatalog:
    return ResponseCatalog.from_dict(
        {
            "company": {
                "intent": "company",
                "instruction": "Select when asked which company.",
                "text": "Bluebird Property Management.",
            },
            "off_script": {
                "intent": "off_script",
                "instruction": "Select only when nothing else fits.",
                "text": "Could we stay focused?",
                "system": True,
            },
        }
    )



def _code_only(fn_or_module) -> str:
    """Source with comments and docstrings removed.

    Two of these assertions are "the module must NOT mention X". But the
    module's own docstring MENTIONS X, precisely to explain that it never
    touches it — so asserting on raw source would fail on the explanation.
    We care about code, not prose.
    """
    import io
    import tokenize

    src = inspect.getsource(fn_or_module)
    out: list[str] = []
    prev_type = tokenize.INDENT
    try:
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type == tokenize.COMMENT:
                continue
            if tok.type == tokenize.STRING and prev_type in (
                tokenize.INDENT,
                tokenize.NEWLINE,
                tokenize.NL,
            ):
                continue  # a docstring
            if tok.type not in (tokenize.NL, tokenize.NEWLINE):
                out.append(tok.string)
            prev_type = tok.type
    except tokenize.TokenError:
        return src
    return " ".join(out)

# ---------------------------------------------------------------------------
# 1. one transport
# ---------------------------------------------------------------------------


def test_every_adapter_resolves_to_the_shared_transport() -> None:
    """Module identity, not a source-text grep.

    A grep would pass while an adapter kept a private `_post` that simply
    called the shared one; identity would not. Conversely identity catches a
    copied loop even when the copy is otherwise identical.
    """
    from livekit_agent_simulator.caller_contract import router_gemini, router_openai
    from livekit_agent_simulator.caller_contract import text_backends

    adapters = {
        "OpenAITextBackend": text_backends.OpenAITextBackend.generate,
        "GeminiTextBackend": text_backends.GeminiTextBackend.generate,
        "OpenAIResponseRouter": router_openai.OpenAIResponseRouter.route,
        "GeminiResponseRouter": router_gemini.GeminiResponseRouter.route,
    }
    # Every adapter must be able to reach the shared function from its module.
    for name, mod in (
        ("text_backends", text_backends),
        ("router_openai", router_openai),
        ("router_gemini", router_gemini),
    ):
        assert hasattr(mod, "post_json"), f"{name} does not import the shared post_json"
        assert mod.post_json is http_json.post_json, (
            f"{name} shadows or re-defines post_json; the transport has been forked"
        )

    # And no adapter may define its own urlopen loop any more.
    for name, fn in adapters.items():
        src = inspect.getsource(fn)
        assert "urlopen" not in src, (
            f"{name} still inlines its own urlopen loop; use http_json.post_json"
        )


def test_text_backends_do_not_retry_and_routers_do() -> None:
    """The divergence is real, load-bearing, and now visible in one place.

    Before the extraction this lived in four copies. It is preserved exactly:
    the text backends never retried and must not start, and the router keeps
    its one retry on 429/5xx while never retrying a 4xx.
    """
    assert inspect.signature(http_json.post_json).parameters["retry_policy"].default is None

    router_src = _code_only(OpenAIResponseRouter._post).replace(" ", "")
    assert "retry_policy=should_retry" in router_src
    assert "max_attempts=2" in router_src

    text_src = _code_only(OpenAITextBackend.generate)
    assert "retry_policy" not in text_src, (
        "passing a retry policy to the text backend would change its behaviour"
    )


# ---------------------------------------------------------------------------
# 2. separate envelopes
# ---------------------------------------------------------------------------


def test_the_text_backend_request_carries_no_schema() -> None:
    """`json_object` and nothing schema-shaped.

    Its response keys are matched lexically in `_parse_backend_response`
    (`_REQUIRED_CANDIDATE_KEYS`), so a real schema would be paid for and
    ignored.
    """
    seen: list[dict] = []

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps(
                {"choices": [{"message": {"content": json.dumps(
                    {"act": "provide", "utterance": "hi", "target": None, "slots": {}}
                )}}]}
            ).encode()

    def _capture(req, timeout=None):
        seen.append(json.loads(req.data.decode("utf-8")))
        return _Resp()

    with patch("urllib.request.urlopen", side_effect=_capture):
        OpenAITextBackend(api_key="sk-x").generate({"current_behavior": {"act": "provide"}})

    body = seen[0]
    rf = body["response_format"]
    assert rf == {"type": "json_object"}, f"text backend envelope changed: {rf}"
    assert "json_schema" not in json.dumps(body), (
        "the text backend must NOT gain a schema; its keys are matched lexically"
    )


def test_the_router_request_carries_the_real_schema() -> None:
    """The counterpart: if this ever loses its schema, an out-of-set id
    becomes structurally possible again — the whole reason the router exists
    is that `additionalProperties: False` makes a wrong label unrepresentable.
    """
    seen: list[dict] = []

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps(
                {"choices": [{"message": {"content": json.dumps({"responseId": "company"})}}]}
            ).encode()

    def _capture(req, timeout=None):
        seen.append(json.loads(req.data.decode("utf-8")))
        return _Resp()

    import asyncio

    with patch("urllib.request.urlopen", side_effect=_capture):
        asyncio.run(
            OpenAIResponseRouter(api_key="sk-x").route(
                agent_transcript="Who are you?", catalog=_catalog()
            )
        )

    rf = seen[0]["response_format"]
    assert rf["type"] == "json_schema"
    assert rf["json_schema"]["schema"]["additionalProperties"] is False
    assert seen[0]["messages"], "the router still sends the routing prompt"


def test_both_envelopes_are_built_by_their_own_adapter_not_by_http_json() -> None:
    """The extraction stopped at the transport, by construction.

    `http_json` must not know what a `response_format` is. If it ever starts
    inspecting or writing one, the separation has been lost.
    """
    src = _code_only(http_json)
    assert "response_format" not in src
    assert "json_schema" not in src


def test_gemini_sends_no_authorization_header() -> None:
    """Gemini carries its key in the endpoint URL.

    The shared `build_request` handles that with `auth_style="raw"`; if it
    regressed to always attaching a bearer header, Gemini would reject every
    call with an auth error rather than anything legible.
    """
    seen: list[dict] = []

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            # Gemini returns the candidate's TEXT, which is then parsed as JSON
            # by `_parse_json_object` — so the text itself must be JSON, not
            # a bare greeting.
            return json.dumps(
                {
                    "candidates": [
                        {
                            "content": {
                                "parts": [
                                    {
                                        "text": json.dumps(
                                            {
                                                "act": "provide",
                                                "utterance": "hi",
                                                "target": None,
                                                "slots": {},
                                            }
                                        )
                                    }
                                ]
                            }
                        }
                    ]
                }
            ).encode()

    def _capture(req, timeout=None):
        seen.append({k.lower(): v for k, v in req.header_items()})
        return _Resp()

    with patch("urllib.request.urlopen", side_effect=_capture):
        GeminiTextBackend(api_key="k").generate({"current_behavior": {"act": "provide"}})

    assert "authorization" not in seen[0], "Gemini must not receive a bearer header"


def test_a_retry_rereads_the_error_body() -> None:
    """An `HTTPError` holds a stream that is consumed once.

    A retry that reused an already-read detail would blow up inside
    `json.loads` and surface as a transport bug instead of the provider's real
    message — the failure would look like the wrong thing entirely.
    """
    import io
    import urllib.error

    reads: list[int] = []

    def _fake_http_error() -> urllib.error.HTTPError:
        err = urllib.error.HTTPError(
            "https://example.invalid/x", 503, "Service Unavailable", {}, io.BytesIO(b'{"e":1}')
        )
        original = err.read

        def _counted(*a, **k):
            reads.append(1)
            return original(*a, **k)

        err.read = _counted  # type: ignore[method-assign]
        return err

    def _always_503(req, timeout=None):
        raise _fake_http_error()

    from livekit_agent_simulator.caller_contract.router import RouterError

    with patch("urllib.request.urlopen", side_effect=_always_503):
        with pytest.raises(RouterError):
            http_json.post_json(
                request=http_json.build_request(
                    url="https://example.invalid/x", body={}, api_key="k"
                ),
                timeout_s=1.0,
                error_factory=lambda d: RouterError(d),
                retry_policy=lambda status: status >= 500,
                max_attempts=2,
            )

    assert len(reads) == 2, f"the error body must be re-read per attempt, got {len(reads)}"
