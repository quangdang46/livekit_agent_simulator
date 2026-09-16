# demo/tools — static showcase pipeline

```
demo/website/                    # GitHub Pages root (publish this folder)
  index.html                     # CASES JSON inlined (~14KB), works on file:// + Pages
  cases/<case-id>/               # one folder per showcase (report-shaped)
    case.json                    # showcase-only layer: toc/title/color/copy/chips
    cues.json                    # report payload — SAME body as build_cues_payload()
    meta.json                    # minimal report meta (run_id, scenario_id, persona)
    conversation.wav             # per-case audio (never shared)

demo/tools/
  build.py     inline cases/*.json into index.html (idempotent, re-runnable)
```

## report parity

`cues.json` uses the exact keys of `build_cues_payload()` in
`src/livekit_agent_simulator/web/cues.py`:

`run_id scenario_id audio{file,duration_ms} cues[] markers[]
marker_counts script_verify assert_verify caller behavior_summary
tool_events[] tool_summary session_summary chat_history`

`case.json` is the only demo-invented file (title/copy/color/chips for the
capability tour). Everything else is copy-pasteable from a real report dir.

## workflows

```bash
python demo/tools/build.py   # after adding/editing any case; always before push
```

Add a case: copy a folder under `demo/website/cases/`, edit `case.json`,
drop `cues.json` + `conversation.wav`, run `build.py`.

Real run: copy its `cues.json` + `conversation.wav` into a new case folder,
keep a small `case.json` (title/copy/color), run `build.py`. Done.
