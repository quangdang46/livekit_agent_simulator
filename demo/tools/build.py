"""Build the static Pages showcase: validate + inline case JSON into index.html.

Reads:  demo/website/cases/<id>/{case.json, cues.json, meta.json, conversation.wav}
        demo/website/cases/manifest.json
        demo/website/index.html   (template with /*__CASES__*/ marker, or prior build)
Writes: demo/website/index.html   (CASES inlined as `var CASES=[...]`)

Why inline: file:// blocks fetch(), so double-clicking index.html shows
"Could not load cases". Inlined JSON (~14KB) renders on file:// AND Pages.
Audio stays as separate cases/<id>/conversation.wav files (must stream).

Idempotent: replaces the existing `var CASES=[...];` blob if present,
else fills the /*__CASES__*/ marker. Safe to re-run.

Adding a case (copy-paste a real run, no index.html edits):
  1. copy a real report's cues.json + conversation.wav into cases/<new-id>/
     (cues.json MUST carry the 16 build_cues_payload() keys — validated below)
  2. write small case.json (toc/title/color/copy/chips) + meta.json
  3. append {"id","toc","duration_ms"} to cases/manifest.json (sorted by id)
  4. python demo/tools/build.py

Usage: python demo/tools/build.py
"""

import json
import re
import subprocess
from pathlib import Path

ROOT = Path("demo/website")
CASES_DIR = ROOT / "cases"
INDEX = ROOT / "index.html"
MARKER = "/*__CASES__*/"
BLOB_RE = re.compile(r"var CASES=\[.*?\];", re.DOTALL)

REAL_KEYS = ["run_id", "scenario_id", "audio", "cues", "markers",
             "marker_counts", "script_verify", "assert_verify", "caller",
             "behavior_summary", "caller_contract", "tool_events",
             "tool_summary", "session_summary", "chat_history", "observe_gaps"]
META_KEYS = ["id", "toc", "title", "color", "copy", "chips"]


def main() -> None:
    manifest = json.loads((CASES_DIR / "manifest.json").read_text(encoding="utf-8"))
    ids = [c["id"] for c in manifest["cases"]]
    assert ids == sorted(ids), "manifest: keep cases sorted by id"
    assert len(ids) == len(set(ids)), "manifest: duplicate ids"

    cases = []
    for entry in manifest["cases"]:
        cid = entry["id"]
        d = CASES_DIR / cid
        assert d.is_dir(), f"{cid}: folder missing"
        for f in ("case.json", "cues.json", "meta.json", "conversation.wav"):
            assert (d / f).exists(), f"{cid}: missing {f}"
        meta = json.loads((d / "case.json").read_text(encoding="utf-8"))
        missing = [k for k in META_KEYS if k not in meta]
        assert not missing, f"{cid}/case.json missing {missing}"
        cues = json.loads((d / "cues.json").read_text(encoding="utf-8"))
        missing = [k for k in REAL_KEYS if k not in cues]
        extra = [k for k in cues if k not in REAL_KEYS]
        assert not missing, f"{cid}/cues.json missing keys {missing}"
        assert not extra, f"{cid}/cues.json extra keys {extra} (not from a real run?)"
        dur = cues["audio"]["duration_ms"]
        assert entry.get("duration_ms") == dur, \
            f"{cid}: manifest duration_ms {entry.get('duration_ms')} != cues {dur}"
        cases.append({"id": cid, "meta": meta, "cues": cues})
        print(f"  {cid}: OK cues={len(cues['cues'])} "
              f"markers={len(cues['markers'])} tools={len(cues['tool_events'])}")

    html = INDEX.read_text(encoding="utf-8")
    blob = "var CASES=" + json.dumps(cases, ensure_ascii=False) + ";"
    if BLOB_RE.search(html):
        html = BLOB_RE.sub(lambda _: blob, html, count=1)
    elif MARKER in html:
        html = html.replace(MARKER, blob, 1)
    else:
        raise AssertionError("neither CASES blob nor /*__CASES__*/ marker in index.html")
    INDEX.write_text(html, encoding="utf-8")
    print(f"inlined {len(cases)} cases ({len(blob)//1024}KB JSON) -> {INDEX}")

    blocks = re.findall(r"<script>(.*?)</script>", html, re.DOTALL)
    js = next(b for b in reversed(blocks) if "renderCase" in b)
    try:
        subprocess.run(["node", "-e", f"new Function({js!r})"],
                       check=True, capture_output=True, text=True)
        print("  index.html JS: SYNTAX OK")
    except FileNotFoundError:
        print("  index.html JS: node unavailable, skipped syntax check")


if __name__ == "__main__":
    main()
