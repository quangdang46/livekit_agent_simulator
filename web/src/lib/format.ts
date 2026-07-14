export function fmtMs(ms: number): string {
  const s = Math.max(0, ms) / 1000;
  const m = Math.floor(s / 60);
  const r = (s % 60).toFixed(1).padStart(4, "0");
  return `${m}:${r}`;
}

export function fmtRecoveryMs(ms: number): string {
  if (ms < 1000) return `${ms}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}

export function truncateLines(text: string, maxLines: number): string {
  const lines = text.split("\n");
  if (lines.length <= maxLines) return text;
  return lines.slice(0, maxLines).join("\n") + "\n…";
}

/** Prefer meta.started_utc, else stamp inside run_id, else mtime. */
export function runInstant(r: {
  run_id: string;
  started_utc?: string | null;
  mtime_ms?: number;
}): Date | null {
  if (r.started_utc) {
    const d = new Date(r.started_utc);
    if (!Number.isNaN(d.getTime())) return d;
  }
  const m = r.run_id.match(/-(\d{8})-(\d{6})-/);
  if (m) {
    const y = m[1].slice(0, 4);
    const mo = m[1].slice(4, 6);
    const day = m[1].slice(6, 8);
    const hh = m[2].slice(0, 2);
    const mm = m[2].slice(2, 4);
    const ss = m[2].slice(4, 6);
    const d = new Date(`${y}-${mo}-${day}T${hh}:${mm}:${ss}Z`);
    if (!Number.isNaN(d.getTime())) return d;
  }
  if (r.mtime_ms && r.mtime_ms > 0) return new Date(r.mtime_ms);
  return null;
}

export function fmtRelative(d: Date | null, now = new Date()): string {
  if (!d) return "unknown time";
  const sec = Math.round((now.getTime() - d.getTime()) / 1000);
  if (sec < 10) return "just now";
  if (sec < 60) return `${sec}s ago`;
  const min = Math.floor(sec / 60);
  if (min < 60) return `${min}m ago`;
  const hr = Math.floor(min / 60);
  if (hr < 24) return `${hr}h ago`;
  const days = Math.floor(hr / 24);
  if (days < 7) return `${days}d ago`;
  return d.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function fmtDuration(ms?: number | null): string | null {
  if (ms == null || Number.isNaN(ms)) return null;
  if (ms < 1000) return `${Math.round(ms)}ms`;
  const s = ms / 1000;
  if (s < 60) return `${s.toFixed(1)}s`;
  const m = Math.floor(s / 60);
  const r = Math.round(s % 60);
  return `${m}m ${r}s`;
}

export function statusTone(status?: string | null): "ok" | "fail" | "warn" | "muted" {
  const s = (status || "").toLowerCase();
  if (s === "passed" || s === "pass" || s === "ok" || s === "done" || s === "success") {
    return "ok";
  }
  if (s === "failed" || s === "fail" || s === "error") return "fail";
  if (s === "running" || s === "pending" || s === "timeout") return "warn";
  return "muted";
}

export function shortRunId(runId: string): string {
  const m = runId.match(/-(\d{8}-\d{6}-[a-f0-9]+)$/i);
  if (m) return m[1];
  return runId.length > 28 ? "…" + runId.slice(-24) : runId;
}
