import type { RunSummary } from "../types";

/** Poll interval while the tab is visible (stdlib HTTP server — no SSE/WS). */
export const RUNS_POLL_MS = 3000;

/**
 * Stable fingerprint so we only re-render when runs appear or change.
 *
 * Must include `tool_count` alongside `turn_count`: while a run is live,
 * `turn_count` only moves when a full turn finalizes, but tool calls land
 * much earlier (backend delegation round-trip before the agent speaks).
 * Without it, a run doing tool work with no new finalized turn looks
 * identical poll-to-poll and the home list never refreshes until the run
 * ends. Same reason `duration_ms` is included (grows as audio is recorded).
 */
export function runsFingerprint(runs: RunSummary[]): string {
  return [...runs]
    .map(
      (r) =>
        `${r.run_id}\t${r.status ?? ""}\t${r.mtime_ms ?? 0}\t${r.duration_ms ?? ""}\t${r.turn_count ?? ""}\t${r.tool_count ?? ""}`,
    )
    .sort()
    .join("\n");
}
