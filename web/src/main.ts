import "./style.css";
import { fetchRuns } from "./api";
import {
  renderRunList,
  type RunListHandle,
} from "./components/run-list";
import {
  renderRunSidebar,
  type RunSidebarHandle,
} from "./components/run-sidebar";
import { showPlayer } from "./player/show-player";
import { runFromUrl, setRunInUrl } from "./lib/url";
import { RUNS_POLL_MS, runsFingerprint } from "./lib/runs-poll";
import type { RunSummary } from "./types";

const appRoot = document.querySelector<HTMLDivElement>("#app");
if (!appRoot) throw new Error("#app missing");
const app: HTMLElement = appRoot;

let playerListeners: AbortController | null = null;
let cachedRuns: RunSummary[] = [];
let lastFingerprint = "";
let view: "list" | "player" = "list";
let activeRunId: string | null = null;
let listHandle: RunListHandle | null = null;
let sidebarHandle: RunSidebarHandle | null = null;
let pollTimer: number | null = null;
let pollInFlight = false;

async function loadRuns(): Promise<RunSummary[]> {
  cachedRuns = await fetchRuns();
  lastFingerprint = runsFingerprint(cachedRuns);
  return cachedRuns;
}

function applyRunsToView(runs: RunSummary[]): void {
  if (view === "list" && listHandle) {
    listHandle.update(runs);
  } else if (view === "player" && sidebarHandle) {
    sidebarHandle.update({ runs, activeRunId });
  }
}

/** Fetch runs; re-render list/sidebar only when the fingerprint changes. */
async function pollRuns(): Promise<void> {
  if (pollInFlight || document.visibilityState === "hidden") return;
  pollInFlight = true;
  try {
    const runs = await fetchRuns();
    const fp = runsFingerprint(runs);
    if (fp === lastFingerprint) return;
    lastFingerprint = fp;
    cachedRuns = runs;
    applyRunsToView(runs);
  } catch {
    /* transient network — keep last good list */
  } finally {
    pollInFlight = false;
  }
}

function startPolling(): void {
  if (pollTimer != null) return;
  pollTimer = window.setInterval(() => {
    void pollRuns();
  }, RUNS_POLL_MS);
}

function ensurePlayerLayout(): { sidebar: HTMLElement; main: HTMLElement } {
  let shell = app.querySelector<HTMLElement>(".app-shell");
  if (!shell) {
    app.innerHTML = `
      <div class="app-shell">
        <div class="app-sidebar" id="app-sidebar"></div>
        <div class="app-main" id="app-main"></div>
      </div>
    `;
    shell = app.querySelector(".app-shell");
  }
  const sidebar = app.querySelector<HTMLElement>("#app-sidebar");
  const main = app.querySelector<HTMLElement>("#app-main");
  if (!sidebar || !main) throw new Error("app shell missing");
  return { sidebar, main };
}

async function showList(): Promise<void> {
  playerListeners?.abort();
  playerListeners = null;
  view = "list";
  activeRunId = null;
  sidebarHandle = null;
  try {
    const runs = await loadRuns();
    app.innerHTML = "";
    listHandle = renderRunList(app, runs, (runId) => {
      setRunInUrl(runId);
      void openPlayer(runId);
    });
  } catch (e) {
    listHandle = null;
    app.innerHTML = `<main class="page"><p class="error">${String(e)}</p></main>`;
  }
}

function paintSidebar(sidebar: HTMLElement, runId: string): void {
  sidebarHandle = renderRunSidebar(sidebar, {
    runs: cachedRuns,
    activeRunId: runId,
    onSelect: (nextId) => {
      setRunInUrl(nextId);
      void openPlayer(nextId);
    },
    onHome: () => {
      setRunInUrl(null);
      void showList();
    },
  });
}

async function openPlayer(runId: string): Promise<void> {
  playerListeners?.abort();
  playerListeners = new AbortController();
  const signal = playerListeners.signal;

  view = "player";
  activeRunId = runId;
  listHandle = null;

  if (!cachedRuns.length) {
    try {
      await loadRuns();
    } catch {
      /* sidebar can stay empty; player still loads */
    }
  }

  const { sidebar, main } = ensurePlayerLayout();
  // Remount sidebar when switching runs so active highlight + shell stay in sync.
  paintSidebar(sidebar, runId);

  await showPlayer(main, runId, signal, () => {
    playerListeners?.abort();
    playerListeners = null;
    setRunInUrl(null);
    void showList();
  });
}

async function boot(): Promise<void> {
  const run = runFromUrl();
  if (run) await openPlayer(run);
  else await showList();
}

window.addEventListener("popstate", () => {
  void boot();
});

document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") void pollRuns();
});

startPolling();
void boot();
