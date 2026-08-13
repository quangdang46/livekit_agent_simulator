import { fmtMs } from "../lib/format";
import { markerTitle } from "../lib/constants";
import { createToolCardElement } from "./tool-card";
import { mountGroupRow, syncGroupExpansion } from "./tool-group";
import type { Cue, Marker, TimelineItem, ToolSpan } from "../types";

function roleLabel(role: string, origin?: string | null): string {
  const r = role.toLowerCase();
  if (r === "agent") return "Agent";
  if (r === "user") {
    if (origin === "script_barge") return "Script inject";
    if (origin === "script_cue") return "Script cue";
    return "Caller";
  }
  return role;
}

function roleClass(role: string, origin?: string | null): string {
  const r = role.toLowerCase();
  if (r === "agent") return "role-agent";
  if (r === "user" && origin === "script_barge") return "role-script-barge";
  if (r === "user" && origin === "script_cue") return "role-script-cue";
  if (r === "user") return "role-user";
  return "role-other";
}

export function buildTimelineItems(
  cues: Cue[],
  markers: Marker[],
  tools: ToolSpan[],
): TimelineItem[] {
  const items: TimelineItem[] = [];
  for (const c of cues) {
    items.push({ kind: "cue", start_ms: c.start_ms, end_ms: c.end_ms, cue: c });
  }
  for (const m of markers) {
    if (m.type === "tool" || m.type === "tool_error") continue;
    items.push({
      kind: "marker",
      start_ms: m.start_ms,
      end_ms: m.end_ms,
      marker: m,
    });
  }
  for (const t of tools) {
    items.push({ kind: "tool", start_ms: t.start_ms, end_ms: t.end_ms, tool: t });
  }
  items.sort((a, b) => {
    if (a.start_ms !== b.start_ms) return a.start_ms - b.start_ms;
    const rank = (item: TimelineItem) => {
      if (item.kind === "marker") return 0;
      if (item.kind === "tool") return 1;
      return 2;
    };
    return rank(a) - rank(b);
  });
  return items;
}

function mountMarkerRow(m: Marker): HTMLLIElement {
  const li = document.createElement("li");
  li.className = `cue-row marker ${m.type}`;
  li.dataset.start = String(m.start_ms);
  li.dataset.end = String(m.end_ms);
  li.innerHTML = `
    <div class="cue-card marker ${m.type}">
      <div class="cue-meta">
        <span class="role marker-type ${m.type}"></span>
        <span class="time"></span>
        <span class="tag ${m.type}"></span>
      </div>
      <div class="cue-text"></div>
      <div class="cue-detail"></div>
    </div>
  `;
  const role = li.querySelector(".role");
  const time = li.querySelector(".time");
  const tag = li.querySelector(".tag");
  const text = li.querySelector(".cue-text");
  const detail = li.querySelector(".cue-detail");
  if (role) role.textContent = markerTitle(m.type);
  if (time) time.textContent = `${fmtMs(m.start_ms)} – ${fmtMs(m.end_ms)}`;
  if (tag) tag.textContent = m.step_id || m.type;
  if (text) text.textContent = m.label + (m.say ? ` · “${m.say}”` : "");
  if (detail) {
    detail.textContent = m.detail || "";
    if (!m.detail) detail.classList.add("hidden");
  }
  return li;
}

function mountCueRow(c: Cue): HTMLLIElement {
  const li = document.createElement("li");
  const r = (c.role || "other").toLowerCase();
  const origin = c.speech_origin || "natural";
  const col = roleClass(r, origin);
  const isScriptInject =
    origin === "script_barge" || origin === "script_cue";
  // A forced caller line (overlay=line) is the CALLER speaking — collapse the
  // script meta under the caller card instead of a separate SCRIPT INJECT card.
  // Audio fixtures (overlay=fixture / barge) stay inject cards.
  const isCallerLine =
    isScriptInject && c.script_overlay === "line" && origin !== "script_barge";
  const isFixture =
    isScriptInject && !isCallerLine;
  li.className = `cue-row ${col}`;
  li.dataset.role = r;
  li.dataset.origin = origin;
  li.dataset.start = String(c.start_ms);
  li.dataset.end = String(c.end_ms);

  li.innerHTML = isFixture
    ? `
    <div class="cue-card ${col} inject-card">
      <div class="script-banner" aria-hidden="true">
        <span class="script-banner-icon">⚡</span>
        <span class="script-banner-title">SCRIPT INJECT</span>
        <span class="script-banner-sub">not Caller · mid-agent cut-in</span>
      </div>
      <div class="cue-meta">
        <span class="role origin-${origin}"></span>
        <span class="time"></span>
        <span class="tags"></span>
      </div>
      <div class="cue-text"></div>
      <div class="cue-detail script-origin"></div>
    </div>
  `
    : `
    <div class="cue-card ${col}">
      <div class="cue-meta">
        <span class="role ${r} origin-${origin}"></span>
        <span class="time"></span>
        <span class="tags"></span>
      </div>
      <div class="cue-text"></div>
      <div class="cue-detail script-origin${isCallerLine ? "" : " hidden"}"></div>
    </div>
  `;

  const role = li.querySelector(".role");
  const time = li.querySelector(".time");
  const text = li.querySelector(".cue-text");
  const tags = li.querySelector(".tags");
  const detail = li.querySelector(".cue-detail.script-origin");
  if (role) role.textContent = roleLabel(c.role, origin);
  if (time) {
    time.textContent = isScriptInject
      ? `inject ${fmtMs(c.inject_ms ?? c.start_ms)} · ${fmtMs(c.start_ms)}–${fmtMs(c.end_ms)}`
      : `${fmtMs(c.start_ms)} – ${fmtMs(c.end_ms)}`;
  }
  if (text) text.textContent = c.text;
  if (tags) {
    if (isFixture) {
      const badge = document.createElement("span");
      badge.className = "tag script_barge";
      badge.textContent =
        origin === "script_barge" ? "barge_in" : "script";
      tags.appendChild(badge);
      if (c.script_overlay) {
        const ov = document.createElement("span");
        ov.className = `tag overlay_${c.script_overlay}`;
        ov.textContent = c.script_overlay;
        tags.appendChild(ov);
      }
      if (c.synthetic) {
        const syn = document.createElement("span");
        syn.className = "tag script_barge";
        syn.textContent = "from Script";
        tags.appendChild(syn);
      }
    } else if (isCallerLine && c.script_overlay) {
      // Caller line: script meta nested under the caller card, not a fixture.
      const ov = document.createElement("span");
      ov.className = `tag overlay_${c.script_overlay}`;
      ov.textContent = c.script_overlay;
      tags.appendChild(ov);
      if (c.script_step_id) {
        const step = document.createElement("span");
        step.className = "tag script_cue";
        step.textContent = `script ${c.script_step_id}`;
        tags.appendChild(step);
      }
    }
    if (c.marker_tags?.length) {
      for (const t of c.marker_tags) {
        if (isScriptInject && t === "barge_in") continue;
        const span = document.createElement("span");
        span.className = `tag ${t}`;
        span.textContent = markerTitle(t);
        tags.appendChild(span);
      }
    }
  }
  if (detail && isScriptInject) {
    detail.textContent = [
      c.script_step_id ? `step: ${c.script_step_id}` : null,
      c.script_say && c.script_say !== c.text
        ? `script say: “${c.script_say}”`
        : null,
      isCallerLine
        ? "forced caller line (Script overlay)"
        : "Do not treat as persona Caller turn",
    ]
      .filter(Boolean)
      .join(" · ");
  }
  return li;
}

export function mountTimelineList(
  ol: HTMLOListElement,
  items: TimelineItem[],
  audio: HTMLAudioElement,
  onUserSeek: () => void,
): HTMLElement[] {
  ol.innerHTML = "";
  const els: HTMLElement[] = [];

  for (const item of items) {
    let li: HTMLLIElement;
    if (item.kind === "marker") {
      li = mountMarkerRow(item.marker);
    } else if (item.kind === "tool") {
      li = createToolCardElement(item.tool);
    } else if (item.kind === "group") {
      li = mountGroupRow(item.group, audio, onUserSeek);
    } else {
      li = mountCueRow(item.cue);
    }

    if (item.kind !== "group") {
      li.addEventListener("click", () => {
        if (!audio.src) return;
        audio.currentTime = (item.start_ms || 0) / 1000;
        onUserSeek();
        void audio.play().catch(() => undefined);
      });
    }
    ol.appendChild(li);
    els.push(li);
  }
  return els;
}

export function activeRank(el: HTMLElement): number {
  const origin = el.dataset.origin || "";
  if (el.classList.contains("role-script-barge") || origin === "script_barge") {
    return 0;
  }
  if (el.classList.contains("marker") && el.classList.contains("barge_in")) {
    return 1;
  }
  if (el.classList.contains("tool-row")) return 2;
  if (el.classList.contains("role-script-cue") || origin === "script_cue") {
    return 3;
  }
  if (el.classList.contains("tool-group-wrap")) return 4;
  if (!el.classList.contains("marker") && !el.classList.contains("tool-row")) {
    return 4;
  }
  return 5;
}

export function findActiveIndex(els: HTMLElement[], tMs: number): number {
  let best = -1;
  let bestRank = 99;
  let bestSpan = Number.POSITIVE_INFINITY;
  let lastStarted = -1;

  for (let i = 0; i < els.length; i++) {
    const start = Number(els[i].dataset.start);
    let end = Number(els[i].dataset.end);
    if (!Number.isFinite(start)) continue;
    if (!Number.isFinite(end) || end <= start) end = start + 900;

    if (tMs >= start) lastStarted = i;

    if (tMs >= start && tMs < end) {
      const span = end - start;
      const rank = activeRank(els[i]);
      if (rank < bestRank || (rank === bestRank && span < bestSpan)) {
        bestRank = rank;
        bestSpan = span;
        best = i;
      }
    }
  }

  if (best >= 0) return best;
  return lastStarted;
}

function setNowBadge(el: HTMLElement, on: boolean): void {
  const card =
    el.querySelector<HTMLElement>(":scope > .cue-card") || el;
  let badge = card.querySelector<HTMLElement>(":scope > .now-badge");
  if (on) {
    if (!badge) {
      badge = document.createElement("span");
      badge.className = "now-badge";
      badge.textContent = "Now";
      card.appendChild(badge);
    }
  } else if (badge) {
    badge.remove();
  }
}

export type FollowState = {
  enabled: boolean;
  suppressScrollUntil: number;
  lastActive: number;
};

export function setFollowUi(btn: HTMLButtonElement, on: boolean): void {
  btn.classList.toggle("on", on);
  btn.classList.toggle("off", !on);
  btn.textContent = on ? "Follow live" : "Follow paused";
  btn.setAttribute("aria-pressed", on ? "true" : "false");
}

export function syncActiveTimeline(
  els: HTMLElement[],
  audio: HTMLAudioElement,
  playhead: HTMLElement,
  durationMs: number,
  follow: FollowState,
): void {
  const t = (audio.currentTime || 0) * 1000;
  const active = findActiveIndex(els, t);

  els.forEach((el, i) => {
    const on = i === active;
    el.classList.toggle("active", on);
    el.setAttribute("aria-current", on ? "true" : "false");
    setNowBadge(el, on);
  });
  syncGroupExpansion(els);

  if (follow.enabled && active >= 0 && active !== follow.lastActive) {
    const el = els[active];
    const dock = document.querySelector(".player-dock") as HTMLElement | null;
    const dockBottom = dock ? dock.getBoundingClientRect().bottom + 12 : 100;
    const rect = el.getBoundingClientRect();
    const inView =
      rect.top >= dockBottom && rect.bottom <= window.innerHeight - 24;
    if (!inView) {
      follow.suppressScrollUntil = performance.now() + 450;
      el.scrollIntoView({ block: "nearest", behavior: "smooth" });
    }
  }
  follow.lastActive = active;

  if (durationMs > 0) {
    const pct = Math.min(100, Math.max(0, (t / durationMs) * 100));
    playhead.style.left = `${pct}%`;
  }
}
