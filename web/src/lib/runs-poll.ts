import type { RunSummary } from "../types";

/** Poll interval while the tab is visible (stdlib HTTP server — no SSE/WS). */
export const RUNS_POLL_MS = 3000;

/** Stable fingerprint so we only re-render when runs appear or change. */
export function runsFingerprint(runs: RunSummary[]): string {
  return [...runs]
    .map(
      (r) =>
        `${r.run_id}\t${r.status ?? ""}\t${r.mtime_ms ?? 0}\t${r.duration_ms ?? ""}\t${r.turn_count ?? ""}`,
    )
    .sort()
    .join("\n");
}
