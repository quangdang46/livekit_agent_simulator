"""Tests for the authored response catalog (response-router v2-3).

The catalog is what makes "the router always returns a valid responseId" a
structural property rather than a hope, so the tests here are mostly about
what the catalog REFUSES to build.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from livekit_agent_simulator.caller_contract.responses import (
    ENUM_LENGTH_LIMIT_FROM,
    MAX_ENUM_TOTAL_CHARS,
    MAX_RESPONSES,
    RESERVED_SYSTEM_ID,
    ResponseCatalog,
    ResponseCatalogError,
)

MODULE = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "livekit_agent_simulator"
    / "caller_contract"
    / "responses.py"
)


def _r(intent: str, text: str = "t", **kw) -> dict:
    return {"intent": intent, "instruction": f"Provide {intent}.", "text": text, **kw}


def _cat(**extra) -> dict:
    base = {"sys": {**_r("off_script", "stay focused?", system=True)}}
    base.update(extra)
    return base


# ------------------------------------------------------------------ required


def test_system_entry_is_required():
    with pytest.raises(ResponseCatalogError, match="must author a system response"):
        ResponseCatalog.from_dict({"a": _r("ask_a")})


def test_exactly_one_system_entry():
    with pytest.raises(ResponseCatalogError, match="system: true"):
        ResponseCatalog.from_dict(_cat(b={**_r("off_script2", "x", system=True)}))


def test_system_entry_without_system_flag_is_not_a_system_entry():
    # An entry that LOOKS like the fallback but is not flagged must not satisfy
    # the requirement, or the catalog builds with no real system entry.
    with pytest.raises(ResponseCatalogError, match="must author a system response"):
        ResponseCatalog.from_dict({"sys": _r("off_script", "stay focused?")})


def test_reserved_id_cannot_be_authored():
    with pytest.raises(ResponseCatalogError, match="reserved"):
        ResponseCatalog.from_dict({RESERVED_SYSTEM_ID: {**_r("off_script", "x", system=True)}})


# -------------------------------------------------------------------- limits


def test_catalog_over_the_count_ceiling_is_a_parse_error():
    # _cat contributes the system entry, so N authored ids make N+1 total.
    at = _cat(**{f"k{i}": _r(f"i{i}") for i in range(MAX_RESPONSES - 1)})
    assert len(at) == MAX_RESPONSES
    assert ResponseCatalog.from_dict(at) is not None  # exactly at the ceiling
    with pytest.raises(ResponseCatalogError, match="enum ceiling"):
        ResponseCatalog.from_dict(_cat(**{f"k{i}": _r(f"i{i}") for i in range(MAX_RESPONSES)}))


def test_enum_length_ceiling_is_also_a_parse_error():
    # Count is legal; joined id length is not. A count-only check would wave
    # this through to a live 400 attributed to the agent under test.
    over = {"k" * 35 + str(i): _r(f"i{i}") for i in range(400)}
    assert sum(len(k) for k in over) > MAX_ENUM_TOTAL_CHARS
    with pytest.raises(ResponseCatalogError, match="character enum limit"):
        ResponseCatalog.from_dict(_cat(**over))


def test_enum_length_ceiling_is_inert_at_or_below_the_threshold():
    # The published rule applies to "more than 250" values. Applying it
    # unconditionally would reject catalogs the providers accept — so build
    # one that would BLOW the char limit if the rule fired at 250, and assert
    # it is still accepted. _cat contributes the system entry, so N authored
    # ids make N+1 total; N=249 lands the catalog exactly ON the threshold.
    authored = ENUM_LENGTH_LIMIT_FROM - 1
    per_id = MAX_ENUM_TOTAL_CHARS // ENUM_LENGTH_LIMIT_FROM + 5  # 65
    under = {"k" * (per_id - 3) + str(i): _r(f"i{i}") for i in range(authored)}
    raw = _cat(**under)
    assert len(raw) == ENUM_LENGTH_LIMIT_FROM, "must sit exactly ON the threshold"
    assert sum(len(k) for k in under) > MAX_ENUM_TOTAL_CHARS, "guard would be vacuous if ids were short"
    assert ResponseCatalog.from_dict(raw) is not None


def test_empty_catalog_is_a_parse_error():
    with pytest.raises(ResponseCatalogError, match="empty"):
        ResponseCatalog.from_dict({})


# -------------------------------------------------------------------- shape


def test_unknown_key_is_rejected():
    with pytest.raises(ResponseCatalogError, match="unknown key"):
        ResponseCatalog.from_dict(_cat(a={**_r("ask_a"), "bogus": 1}))


def test_missing_required_field_is_rejected():
    with pytest.raises(ResponseCatalogError, match="missing `text:`"):
        ResponseCatalog.from_dict(_cat(a={"intent": "i", "instruction": "x"}))


def test_duplicate_intent_is_rejected():
    # Two ids sharing an intent make the judge unable to attribute which
    # response was served.
    with pytest.raises(ResponseCatalogError, match="share intent"):
        ResponseCatalog.from_dict(_cat(a=_r("same"), b=_r("same")))


def test_duplicate_intent_colliding_with_the_system_entry_is_rejected():
    with pytest.raises(ResponseCatalogError, match="share intent"):
        ResponseCatalog.from_dict(_cat(a=_r("off_script")))


def test_error_reports_file_and_line():
    with pytest.raises(ResponseCatalogError) as exc:
        ResponseCatalog.from_dict({"a": _r("ask_a")}, file="sc.yaml", line=42)
    assert "sc.yaml:42" in str(exc.value)


# ----------------------------------------------------------------- serve


def test_serve_spends_a_non_reusable_response():
    c = ResponseCatalog.from_dict(_cat(a=_r("ask_a"), b=_r("ask_b", reusable=True)))
    assert c.offerable_ids() == ["sys", "a", "b"]
    c.serve("a")
    assert c.is_spent("a")
    assert "a" not in c.offerable_ids()
    assert c.offerable_ids() == ["sys", "b"]


def test_reusable_response_is_never_spent():
    c = ResponseCatalog.from_dict(_cat(b=_r("ask_b", reusable=True)))
    c.serve("b")
    c.serve("b")
    assert not c.is_spent("b")
    assert c.served_counts() == {"b": 2}
    assert "b" in c.offerable_ids()


def test_option_set_collapses_to_the_system_response_and_never_empties():
    # Running out of authored material is a CATALOG AUTHORING fact, not an
    # agent deviation — the router must still have something valid to return.
    c = ResponseCatalog.from_dict(_cat(a=_r("ask_a")))
    c.serve("a")
    assert c.offerable_ids() == ["sys"]
    c.serve("sys")
    assert c.offerable_ids() == ["sys"], "the system response is always offerable"


def test_serving_an_unknown_id_raises():
    c = ResponseCatalog.from_dict(_cat(a=_r("ask_a")))
    with pytest.raises(ResponseCatalogError, match="not in this catalog"):
        c.serve("nope")


def test_system_id_is_always_offerable_even_when_spent():
    c = ResponseCatalog.from_dict(_cat(a=_r("ask_a")))
    c.serve("sys")
    assert c.system_id in c.offerable_ids()


# ----------------------------------------------------- AGENTS.md boundary


def test_module_carries_no_business_strings():
    # AGENTS.md forbids business text in src/. A canned out-of-scope line baked
    # into the package would be spoken by a simulated caller in a scenario that
    # has nothing to do with it — which is why the system entry is authored.
    source = MODULE.read_text(encoding="utf-8")
    for banned in ("property inquiry", "Bluebird", "555-0", "Dana Whitfield"):
        assert banned.lower() not in source.lower(), f"business string {banned!r} in responses.py"
