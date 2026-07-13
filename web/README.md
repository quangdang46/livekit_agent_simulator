# lk-sim report player (Vite + TypeScript)

Standard Vite layout. Built assets are **not** committed — CI stages `dist/` into
`templates/report-player/` for the Python wheel.

## Dev (HMR)

Terminal 1 — API + reports:

```bash
uv run lk-sim web --root /path/to/target
```

Terminal 2 — frontend:

```bash
pnpm install
pnpm dev
```

Open http://localhost:5173 — proxies `/api` and `/runs` to port 8765.

## Build (maintainers)

```bash
pnpm install
pnpm build          # → web/dist/
```

For a local wheel or to match CI:

```bash
python ../scripts/bundle_report_player.py   # → templates/report-player/
```

`lk-sim web` also accepts `web/dist/` directly in an editable checkout (no bundle step).

## Layout

```
web/
  index.html
  public/
  src/
    main.ts
    components/
    lib/
    player/
    types.ts
    api.ts
    style.css
  dist/             # gitignored — vite build output
```
