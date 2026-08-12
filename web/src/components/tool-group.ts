import { fmtMs } from "../lib/format";
import { markerTitle } from "../lib/constants";
import { createToolCardElement } from "./tool-card";
import type { Marker, TimelineItem, ToolSpan } from "../types";

const GROUP_SOFT_CAP = 4;
const HARD_CAP = 12;
const MAX_CONSECUTIVE_MS = 3500;
const WRAP_RADIUS_MS = 2200;
const TIGHT_MAX_GAP_MS = 1200;

type GroupKind =
  | { kind: "tool"; key: string; name: string }
  | { kind: "marker"; key: string; type: string };

type GroupInfo = {
  kind: GroupKind;
  items: TimelineItem[];
  start_ms: number;
  end_ms: number;
};

function markerFrom(item: TimelineItem): Marker | null {
  return item.kind === "marker" ? item.marker : null;
}

function toolFrom(item: TimelineItem): ToolSpan | null {
  return item.kind === "tool" ? item.tool : null;
}

function groupKey(item: TimelineItem): GroupKind | null {
  const t = toolFrom(item);
  if (t) return { kind: "tool", key: `tool:${t.name}`, name: t.name };
  const m = markerFrom(item);
  if (m) return { kind: "marker", key: `marker:${m.type}`, type: m.type };
  return null;
}

function groupLabel(kind: GroupKind): string {
  return kind.kind === "tool" ? `Tool · ${kind.name}` : markerTitle(kind.type);
}

/** Group same-kind (tool name / marker type) runs into collapsible clusters. */
export function groupTimelineItems(items: TimelineItem[]): TimelineItem[] {
  const out: TimelineItem[] = [];
  let open: GroupInfo | null = null;

  for (const item of items) {
    const kind = groupKey(item);
    if (!kind) {
      flush(out, open);
      open = null;
      out.push(item);
      continue;
    }

    const start = item.start_ms;
    const end = item.end_ms;

    if (open && open.kind.key !== kind.key) {
      flush(out, open);
      open = null;
    }

    if (!open) {
      open = {
        kind,
        items: [item],
        start_ms: start,
        end_ms: end,
      };
      continue;
    }

    const gap = start - open.end_ms;
    const last = open.items[open.items.length - 1];

    if (gap <= 0) {
      open.items.push(item);
      open.end_ms = Math.max(open.end_ms, end);
      continue;
    }

    const isTight = gap <= TIGHT_MAX_GAP_MS;
    const isSameTurn =
      (last.kind === "tool" && item.kind === "tool" && last.tool.turn === item.tool.turn) ||
      (last.kind === "marker" && item.kind === "marker" && last.marker.step_id != null && last.marker.step_id === item.marker.step_id);
    const canWrap =
      (isTight && isSameTurn) || gap <= WRAP_RADIUS_MS;

    if (open.items.length < HARD_CAP && canWrap) {
      open.items.push(item);
      open.end_ms = Math.max(open.end_ms, end);
    } else {
      flush(out, open);
      open = { kind, items: [item], start_ms: start, end_ms: end };
    }
  }
  flush(out, open);
  return out;
}

function flush(out: TimelineItem[], group: GroupInfo | null): void {
  if (!group) return;
  if (group.items.length === 1) {
    out.push(group.items[0]);
    return;
  }
  out.push({ kind: "group", start_ms: group.start_ms, end_ms: group.end_ms, group });
}

function countErrors(group: GroupInfo): number {
  return group.items.reduce(
    (acc, it) => (it.kind === "tool" && it.tool.is_error ? acc + 1 : acc),
    0,
  );
}

function setGroupExpanded(
  el: HTMLElement,
  expanded: boolean,
  count: number,
): void {
  const details = el.querySelector<HTMLDetailsElement>("details.tool-group");
  if (!details) return;
  details.open = expanded;
  const summary = el.querySelector(".tool-group-summary");
  if (summary) {
    summary.setAttribute("aria-expanded", String(expanded));
    summary.textContent = expanded
      ? `Hide ${count} ${count === 1 ? "item" : "items"}`
      : `Show ${count} ${count === 1 ? "item" : "items"}`;
  }
}

/**
 * Auto-expand the group card containing the active timeline item so the
 * highlighted event stays visible while the audio plays.
 */
export function syncGroupExpansion(els: HTMLElement[]): void {
  for (const el of els) {
    if (!el.classList.contains("tool-group-wrap")) continue;
    const details = el.querySelector<HTMLDetailsElement>("details.tool-group");
    if (!details) continue;
    const count = Number(el.dataset.groupCount || 0);
    // Only auto-open the active group; never auto-close a group the user
    // expanded manually (keeps follow behavior without fighting the user).
    if (el.classList.contains("active") && !details.open) {
      setGroupExpanded(el, true, count);
    }
  }
}

function mountGroupBody(group: GroupInfo, audio: HTMLAudioElement, onUserSeek: () => void): HTMLElement {
  const body = document.createElement("div");
  body.className = "tool-group-body";

  for (const item of group.items) {
    const child: HTMLElement =
      item.kind === "tool"
        ? createToolCardElement(item.tool)
        : document.createElement("li");
    if (item.kind === "marker") {
      child.innerHTML = `
        <div class="cue-card marker ${item.marker.type}">
          <div class="cue-meta">
            <span class="role marker-type ${item.marker.type}"></span>
            <span class="time"></span>
            <span class="tag ${item.marker.type}"></span>
          </div>
          <div class="cue-text"></div>
          <div class="cue-detail"></div>
        </div>
      `;
      const role = child.querySelector(".role");
      const time = child.querySelector(".time");
      const tag = child.querySelector(".tag");
      const text = child.querySelector(".cue-text");
      const detail = child.querySelector(".cue-detail");
      const m = item.marker;
      if (role) role.textContent = markerTitle(m.type);
      if (time) time.textContent = `${fmtMs(m.start_ms)} – ${fmtMs(m.end_ms)}`;
      if (tag) tag.textContent = m.step_id || m.type;
      if (text) text.textContent = m.label + (m.say ? ` · “${m.say}”` : "");
      if (detail) {
        detail.textContent = m.detail || "";
        if (!m.detail) detail.classList.add("hidden");
      }
      child.classList.add("grouped-marker");
      child.classList.add(`marker-${m.type}`);
    }
    child.addEventListener("click", () => {
      if (!audio.src) return;
      audio.currentTime = (item.start_ms || 0) / 1000;
      onUserSeek();
      void audio.play().catch(() => undefined);
    });
    body.appendChild(child);
  }
  return body;
}

function mountGroupSummary(
  group: GroupInfo,
  count: number,
  errCount: number,
): HTMLButtonElement {
  const kind = group.kind;
  const summary = document.createElement("button");
  summary.type = "button";
  summary.className = "tool-group-summary";

  const label = document.createElement("span");
  label.className = `tool-group-label ${kind.kind === "tool" ? "tool" : kind.type}`;
  const prefix = kind.kind === "tool" ? "🔧" : "";
  const toolCount = kind.kind === "tool" ? ` ×${count}` : "";
  label.textContent = `${prefix} ${groupLabel(kind)}${toolCount}`;

  const time = document.createElement("span");
  time.className = "time";
  time.textContent = `${fmtMs(group.start_ms)} – ${fmtMs(group.end_ms)}`;

  const countBadge = document.createElement("span");
  countBadge.className = "tool-group-count";
  countBadge.textContent = `${count} ${count === 1 ? "item" : "items"}`;

  summary.append(label, time, countBadge);

  if (errCount > 0) {
    const err = document.createElement("span");
    err.className = "tool-group-err";
    err.textContent = `${errCount} error${errCount === 1 ? "" : "s"}`;
    summary.appendChild(err);
  }
  return summary;
}

/** Build the grouped `<details>` card for a group cluster. */
export function mountGroupRow(
  group: GroupInfo,
  audio: HTMLAudioElement,
  onUserSeek: () => void,
): HTMLLIElement {
  const li = document.createElement("li");
  li.className = "cue-row tool-group-wrap";
  li.dataset.start = String(group.start_ms);
  li.dataset.end = String(group.end_ms);

  const count = group.items.length;
  const errCount = countErrors(group);
  const autoExpand = group.kind.kind === "marker" && count < GROUP_SOFT_CAP;
  li.dataset.groupCount = String(count);

  const details = document.createElement("details");
  details.className = "tool-group";
  details.open = autoExpand;

  const summary = mountGroupSummary(group, count, errCount);
  summary.addEventListener("click", (ev) => {
    ev.stopPropagation();
    details.open = !details.open;
    setGroupExpanded(li, details.open, count);
  });

  const header = document.createElement("summary");
  header.className = "tool-group-head";
  header.textContent = "";
  header.appendChild(summary);

  const body = mountGroupBody(group, audio, onUserSeek);
  details.append(header, body);
  li.append(details);
  return li;
}

export { MAX_CONSECUTIVE_MS, GROUP_SOFT_CAP };
