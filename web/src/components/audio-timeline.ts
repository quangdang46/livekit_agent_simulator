import { fmtMs } from "../lib/format";
import { markerTitle } from "../lib/constants";
import type { Marker } from "../types";

/** Metadata markers that should not appear as audio timeline bands. */
const HIDDEN_BAND_TYPES = new Set([
  "audio_onset",
  "user_audio_source",
  "agent_audio_onset",
]);

export function mountAudioTimeline(
  timeline: HTMLElement,
  playhead: HTMLElement,
  markers: Marker[],
  durationMs: number,
  audio: HTMLAudioElement,
): void {
  for (const node of Array.from(timeline.querySelectorAll(".timeline-band"))) {
    node.remove();
  }
  const dur = Math.max(durationMs, 1);
  for (const m of markers) {
    // Skip metadata markers — they're noise in the audio timeline
    if (HIDDEN_BAND_TYPES.has(m.type)) continue;

    const band = document.createElement("button");
    band.type = "button";
    band.className = `timeline-band ${m.type}`;
    const left = (m.start_ms / dur) * 100;
    const width = Math.max(0.4, ((m.end_ms - m.start_ms) / dur) * 100);
    band.style.left = `${left}%`;
    band.style.width = `${width}%`;
    band.title = `${markerTitle(m.type)}: ${m.label}\n${fmtMs(m.start_ms)} – ${fmtMs(m.end_ms)}${m.detail ? "\n" + m.detail : ""}`;
    band.addEventListener("click", (ev) => {
      ev.stopPropagation();
      if (!audio.src) return;
      audio.currentTime = (m.start_ms || 0) / 1000;
      void audio.play().catch(() => undefined);
    });
    timeline.appendChild(band);
  }
  timeline.appendChild(playhead);

  timeline.onclick = (ev) => {
    if (!audio.src || !durationMs) return;
    const rect = timeline.getBoundingClientRect();
    const x = (ev as MouseEvent).clientX - rect.left;
    const ratio = Math.min(1, Math.max(0, x / rect.width));
    audio.currentTime = (ratio * durationMs) / 1000;
    void audio.play().catch(() => undefined);
  };
}
