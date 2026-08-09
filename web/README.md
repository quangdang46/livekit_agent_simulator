# lks web UI (Vite + TypeScript)

Standard Vite layout. CI builds `web/dist/` and Hatch force-includes it into the
wheel as `livekit_agent_simulator/web_static/`. Built assets are **not** committed.

## Dev (HMR)

Terminal 1 — API + reports:

```bash
uv run lks web --root /path/to/target
```

Terminal 2 — frontend:

```bash
pnpm install
pnpm dev
```

Open http://localhost:5173 — proxies `/api` and `/runs` to port 8765.

The home list and player sidebar **poll** `GET /api/runs` every ~3s (paused when
the tab is hidden) and re-render only when run ids / status / mtime change.

## Build (maintainers)

```bash
pnpm install
pnpm build          # → web/dist/
```

Then `uv build` (or release CI) packs `web/dist` into the wheel as `web_static`.
Editable checkouts also serve `web/dist` directly via `lks web`.

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
