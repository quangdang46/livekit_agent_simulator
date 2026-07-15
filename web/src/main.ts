import "./style.css";
import { fetchRuns } from "./api";
import { renderRunList } from "./components/run-list";
import { renderRunSidebar } from "./components/run-sidebar";
import { showPlayer } from "./player/show-player";
import { runFromUrl, setRunInUrl } from "./lib/url";
import type { RunSummary } from "./types";

const appRoot = document.querySelector<HTMLDivElement>("#app");
if (!appRoot) throw new Error("#app missing");
const app: HTMLElement = appRoot;

let playerListeners: AbortController | null = null;
let cachedRuns: RunSummary[] = [];

async function loadRuns(): Promise<RunSummary[]> {
  cachedRuns = await fetchRuns();
  return cachedRuns;
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
  try {
    const runs = await loadRuns();
    app.innerHTML = "";
    renderRunList(app, runs, (runId) => {
      setRunInUrl(runId);
      void openPlayer(runId);
    });
  } catch (e) {
    app.innerHTML = `<main class="page"><p class="error">${String(e)}</p></main>`;
  }
}

function paintSidebar(
  sidebar: HTMLElement,
  activeRunId: string,
): void {
  renderRunSidebar(sidebar, {
    runs: cachedRuns,
    activeRunId,
    onSelect: (runId) => {
      setRunInUrl(runId);
      void openPlayer(runId);
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

  if (!cachedRuns.length) {
    try {
      await loadRuns();
    } catch {
      /* sidebar can stay empty; player still loads */
    }
  }

  const { sidebar, main } = ensurePlayerLayout();
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

void boot();
