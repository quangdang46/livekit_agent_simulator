"""Tier-3 semantic verifier: evidence-constrained LLM judge (text-only).

Implements ``SemanticVerifierProtocol`` (see validator.py):
``classify(utterance, contract) -> ObservedAct`` derived from the UTTERANCE
TEXT ALONE — never trusts generator claims (the classifier is not even given
the candidate).

Boundary (same as text_backends.py): single stateless HTTP round trip via
stdlib ``urllib`` only. No audio/Realtime session, no publish path, no caller
flow control. A transport/parse failure raises (never fabricates a confident
answer) so the validator maps it to ERROR, never VALID.

Evidence constraint: the prompt requires the model to quote the utterance
span supporting its act/target judgment. If the quoted evidence is not found
verbatim in the utterance, confidence is capped below the validator's
``SEMANTIC_CONFIDENCE_THRESHOLD`` (0.5) so the verdict fails CLOSED as
LOW_CONFIDENCE rather than passing on an unsupported claim.

Why Tier-3 before Tier-2a (NLI feasibility note): a cross-encoder NLI gives
entailment scores for (premise, hypothesis) pairs, which covers *act*
classification via hypothesis templates — but golden requires two more
capabilities NLI does not provide on its own: (1) *target* extraction from
utterance evidence ("price" vs "delivery_date" is a span/slot judgment, not
an entailment label), and (2) open-vocabulary *all_acts* multi-label output
for nested forbidden intents. Both would need an extra span-extraction +
per-act thresholding layer on top of NLI, i.e. building a semantic parser
anyway. The LLM judge populates act + target + all_acts + confidence in one
call, so it is prototyped first; NLI stays an opt-in only if the golden
benchmark below shows the judge's latency/cost is unacceptable.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from . import BehaviorContract, ObservedAct

_JUDGE_SYSTEM_PROMPT = """You are a strict semantic classifier for a simulated phone-caller safety gate. \
You are given ONE caller utterance and its assigned behavior contract \
(behavior, target, forbidden_intents). You re-derive what the utterance \
ACTUALLY does from its text alone.

Respond with a single JSON object, no prose, no markdown fences:
{"act": "<primary SURFACE speech act: negotiate/ask/accept/confirm/deny/provide/end>", \
"target": "<topic of the primary act, or null>", \
"all_acts": ["<primary act plus every other act/intent present, including nested ones>"], \
"confidence": <0.0-1.0>, \
"evidence": "<short verbatim quote from the utterance supporting act/target>"}

Rules:
- act/target come from UTTERANCE EVIDENCE only. Never echo the contract's \
behavior/target unless the utterance itself shows it.
- negotiate vs ask: negotiate is a PROPOSAL or REQUEST TO CHANGE a deal term \
("could you lower the price", "would you consider $30,000", "do you offer \
financing" — offering/asking for an alternative arrangement IS a proposal). \
ask is asking for a FACT with no proposed change ("what's the price?", \
"what are your hours?" — a plain lookup, nothing to grant or refuse). If the \
utterance only requests information and proposes no alternative, it is ask \
even if the fact is a deal term like price.
- Detect these deal-related intents independently of the contract's \
forbidden_intents list (that list only decides what to REJECT, not what to \
recognize): financing (any loan/payment-plan/pay-over-time language), \
trade_in, vehicle_change. Tag them in all_acts by these exact names whenever \
present, paraphrased or not, whether or not the contract forbids them.
- all_acts contains the primary act plus every other surface act AND every \
detected deal-related intent from the list above.
- target is the utterance's ACTUAL topic as a snake_case noun (price, \
delivery_date, financing, ...), derived ONLY from what the utterance itself \
is about. The contract's target is given for context ONLY — it is what the \
caller was SUPPOSED to talk about, not what this utterance IS about. Never \
copy the contract's target string onto the utterance's real topic; if the \
utterance drifted to a different topic (e.g. financing instead of price), \
report THAT topic so a mismatch can be detected downstream. Return null only \
when the utterance truly has no identifiable topic.
- confidence >= 0.5 only when the evidence quote appears verbatim in the \
utterance. Ambiguous filler or off-topic chatter gets confidence <= 0.3."""


def _build_user_prompt(utterance: str, contract: BehaviorContract) -> str:
    return json.dumps(
        {
            "utterance": utterance,
            "contract": {
                "behavior": contract.behavior,
                "target": contract.target,
                "forbidden_intents": list(contract.constraints.forbidden_intents),
            },
        },
        ensure_ascii=False,
    )


def _parse_json_object(raw_text: str) -> dict[str, Any]:
    text = raw_text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SemanticJudgeError(f"judge did not return valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise SemanticJudgeError(f"judge JSON must be an object, got {type(parsed).__name__}")
    return parsed


class SemanticJudgeError(RuntimeError):
    """Transport/parse failure — the validator maps a raise to ERROR (never VALID)."""


def build_observed_act(
    payload: dict[str, Any],
    utterance: str,
    *,
    confidence_cap_without_evidence: float = 0.49,
) -> ObservedAct:
    """Validate a raw judge dict into ObservedAct, enforcing the evidence cap.

    Raises SemanticJudgeError on malformed payloads (missing act, bad
    confidence range, non-list all_acts). Caps confidence below the
    validator threshold when the quoted evidence span is absent from the
    utterance verbatim.
    """
    act = payload.get("act")
    if not isinstance(act, str) or not act.strip():
        raise SemanticJudgeError(f"judge act must be a non-empty string, got {act!r}")
    target = payload.get("target")
    if target is not None and (not isinstance(target, str) or not target.strip()):
        raise SemanticJudgeError(f"judge target must be a string or null, got {target!r}")
    all_acts = payload.get("all_acts", [act])
    if not isinstance(all_acts, list) or not all(isinstance(a, str) for a in all_acts):
        raise SemanticJudgeError(f"judge all_acts must be a string list, got {all_acts!r}")
    try:
        confidence = float(payload.get("confidence", 0.0))
    except (TypeError, ValueError) as exc:
        raise SemanticJudgeError(f"judge confidence must be numeric, got {payload.get('confidence')!r}") from exc
    if not 0.0 <= confidence <= 1.0:
        raise SemanticJudgeError(f"judge confidence must be in [0.0, 1.0], got {confidence!r}")

    evidence = payload.get("evidence", "")
    if not isinstance(evidence, str) or not evidence.strip() or evidence.strip().lower() not in utterance.lower():
        confidence = min(confidence, confidence_cap_without_evidence)

    acts = list(dict.fromkeys([a for a in [act.strip(), *[a.strip() for a in all_acts]] if a]))
    observed = ObservedAct(
        act=act.strip(),
        target=target.strip() if isinstance(target, str) else None,
        confidence=confidence,
        all_acts=acts,
    )
    observed.validate()
    return observed


@dataclass
class LLMSemanticVerifier:
    """Tier-3 judge behind SemanticVerifierProtocol (text-only, stateless).

    provider="openai" uses an OpenAI-compatible chat-completions endpoint;
    provider="anthropic" uses a Messages-API-compatible endpoint (same wire
    format as evals/backends/http_anthropic.py, reused so live_wiring.py can
    mirror whatever endpoint_type the target's judge: block already uses);
    provider="gemini" uses generateContent. All three are one HTTP round trip.
    """

    api_key: str
    provider: str = "openai"
    # NOTE: pinned 2026-09-10 — gemini-2.0-flash / 2.5-flash return 404 on this
    # key ("no longer available"); gemini-flash-latest is the working alias.
    model: str = "gemini-flash-latest"
    base_url: str = ""  # empty = provider default (see __post_init__)
    temperature: float = 0.0
    timeout_s: float = 20.0

    _OPENAI_BASE = "https://api.openai.com/v1"
    _GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"

    def __post_init__(self) -> None:
        if self.base_url:
            return
        if self.provider == "gemini":
            self.base_url = self._GEMINI_BASE
        elif self.provider == "openai":
            self.base_url = self._OPENAI_BASE
        # provider="anthropic" has no public default endpoint (unlike OpenAI/
        # Gemini's hosted APIs) — same as http_anthropic.py, base_url is
        # mandatory and left empty here to fail loudly in classify() rather
        # than silently guessing a wrong host.

    def _openai_endpoint(self) -> str:
        base = self.base_url.rstrip("/")
        if base.endswith("/chat/completions"):
            return base
        return f"{base}/chat/completions"

    def _gemini_endpoint(self) -> str:
        return f"{self.base_url.rstrip('/')}/models/{self.model}:generateContent?key={self.api_key}"

    def _anthropic_endpoint(self) -> str:
        base = self.base_url.rstrip("/")
        if base.endswith("/messages"):
            return base
        return f"{base}/messages"

    def _judge_request(self, utterance: str, contract: BehaviorContract) -> dict[str, Any]:
        user_prompt = _build_user_prompt(utterance, contract)
        if self.provider == "gemini":
            return {
                "url": self._gemini_endpoint(),
                "headers": {"Content-Type": "application/json"},
                "body": {
                    "system_instruction": {"parts": [{"text": _JUDGE_SYSTEM_PROMPT}]},
                    "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
                    "generationConfig": {"temperature": self.temperature, "responseMimeType": "application/json"},
                },
            }
        if self.provider == "anthropic":
            if not self.base_url:
                raise SemanticJudgeError("provider='anthropic' requires an explicit base_url (no public default)")
            return {
                "url": self._anthropic_endpoint(),
                "headers": {
                    "Authorization": f"Bearer {self.api_key}",
                    "x-api-key": self.api_key,
                    "anthropic-version": "2023-06-01",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                "body": {
                    "model": self.model,
                    "max_tokens": 512,
                    "temperature": self.temperature,
                    "stream": False,
                    "system": _JUDGE_SYSTEM_PROMPT,
                    "messages": [{"role": "user", "content": user_prompt}],
                },
            }
        if self.provider != "openai":
            raise SemanticJudgeError(f"unknown provider {self.provider!r} (expected 'openai'|'anthropic'|'gemini')")
        return {
            "url": self._openai_endpoint(),
            "headers": {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            "body": {
                "model": self.model,
                "temperature": self.temperature,
                "stream": False,
                "messages": [
                    {"role": "system", "content": _JUDGE_SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                "response_format": {"type": "json_object"},
            },
        }

    def _extract_content(self, payload: dict[str, Any]) -> str:
        if self.provider == "gemini":
            candidates = payload.get("candidates") or []
            if not candidates:
                raise SemanticJudgeError(f"judge empty candidates: {str(payload)[:300]}")
            parts = ((candidates[0].get("content") or {}).get("parts")) or []
            text = "".join(str(p.get("text") or "") for p in parts if isinstance(p, dict))
        elif self.provider == "anthropic":
            content = payload.get("content")
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                text = "".join(
                    str(part.get("text") or "")
                    for part in content
                    if isinstance(part, dict) and part.get("type") in (None, "text")
                )
            else:
                raise SemanticJudgeError(f"judge empty content: {str(payload)[:300]}")
        else:
            choices = payload.get("choices") or []
            if not choices:
                raise SemanticJudgeError(f"judge empty choices: {str(payload)[:300]}")
            text = (choices[0].get("message") or {}).get("content") or ""
        if not isinstance(text, str) or not text.strip():
            raise SemanticJudgeError("judge returned empty content")
        return text

    def classify(self, utterance: str, contract: BehaviorContract) -> ObservedAct:
        spec = self._judge_request(utterance, contract)
        data = json.dumps(spec["body"]).encode("utf-8")
        req = urllib.request.Request(spec["url"], data=data, method="POST", headers=spec["headers"])
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")[:500]
            raise SemanticJudgeError(f"judge HTTP {e.code}: {err_body}") from e
        except urllib.error.URLError as e:
            raise SemanticJudgeError(f"judge unreachable: {e}") from e
        return build_observed_act(_parse_json_object(self._extract_content(payload)), utterance)


__all__ = ["LLMSemanticVerifier", "SemanticJudgeError", "build_observed_act"]
