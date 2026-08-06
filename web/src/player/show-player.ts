import { fetchCues } from "../api";
import type { CuesPayload } from "../types";
import { mountAudioTimeline } from "../components/audio-timeline";
import { mountLegend } from "../components/legend";
import { renderPlayerShell } from "../components/player-shell";
import { mountSessionFooter } from "../components/session-footer";
import {
  buildTimelineItems,
  mountTimelineList,
  setFollowUi,
  syncActiveTimeline,
  type FollowState,
} from "../components/timeline-list";
import { mountVerifyBar } from "../components/verify-bar";

export function bindFollowControls(
  followBtn: HTMLButtonElement,
  follow: FollowState,
  signal: AbortSignal,
): void {
  setFollowUi(followBtn, true);
  followBtn.addEventListener(
    "click",
    () => {
      follow.enabled = !follow.enabled;
      setFollowUi(followBtn, follow.enabled);
      if (follow.enabled) follow.lastActive = -2;
    },
    { signal },
  );

  const pauseFollowFromUser = () => {
    if (performance.now() < follow.suppressScrollUntil) return;
    if (!follow.enabled) return;
    follow.enabled = false;
    setFollowUi(followBtn, false);
  };

  window.addEventListener("wheel", pauseFollowFromUser, { passive: true, signal });
  window.addEventListener("touchmove", pauseFollowFromUser, {
    passive: true,
    signal,
  });
  window.addEventListener(
    "keydown",
    (ev) => {
      if (
        ev.key === "PageUp" ||
        ev.key === "PageDown" ||
        ev.key === "Home" ||
        ev.key === "End" ||
        ((ev.key === "ArrowUp" || ev.key === "ArrowDown") &&
          !(ev.target instanceof HTMLInputElement) &&
          !(ev.target instanceof HTMLTextAreaElement) &&
          !(ev.target instanceof HTMLSelectElement))
      ) {
        pauseFollowFromUser();
      }
    },
    { signal },
  );
}

export async function showPlayer(
  app: HTMLElement,
  runId: string,
  signal: AbortSignal,
  onBack: () => void,
): Promise<void> {
  const ui = renderPlayerShell(app, runId, onBack);
  const follow: FollowState = {
    enabled: true,
    suppressScrollUntil: 0,
    lastActive: -1,
  };
  bindFollowControls(ui.followBtn, follow, signal);

  try {
    const data = await fetchCues(runId);
    const markers = data.markers || [];
    const tools = data.tool_events || [];
    let durationMs =
      data.audio?.duration_ms != null
        ? Number(data.audio.duration_ms)
        : Math.max(
            0,
            ...markers.map((m) => m.end_ms),
            ...tools.map((t) => t.end_ms),
            ...(data.cues || []).map((c) => c.end_ms),
          ) || 1;

    if (data.scenario_id) {
      ui.subtitle.textContent = `scenario: ${data.scenario_id}`;
    }
    if (data.audio?.file) {
      ui.audio.src = `/runs/${encodeURIComponent(runId)}/${data.audio.file}`;
    } else {
      ui.missing.classList.remove("hidden");
    }

    const behavior =
      data.behavior_summary || data.caller?.behavior_summary || null;

    mountVerifyBar(ui.verify, {
      script: data.script_verify,
      assertV: data.assert_verify,
      counts: data.marker_counts,
      behavior,
      toolSummary: data.tool_summary,
      observeGaps: data.observe_gaps,
    });
    mountLegend(ui.legend, markers);
    mountAudioTimeline(
      ui.timeline,
      ui.playhead,
      markers,
      durationMs,
      ui.audio,
    );
    mountSessionFooter(
      ui.sessionFooter,
      data.session_summary,
      data.chat_history,
    );

    const onUserSeek = () => {
      follow.enabled = true;
      setFollowUi(ui.followBtn, true);
      follow.lastActive = -2;
    };

    const items = buildTimelineItems(data.cues || [], markers, tools);
    const els = mountTimelineList(ui.cuesEl, items, ui.audio, onUserSeek);
    if (!els.length) {
      ui.subtitle.textContent =
        (ui.subtitle.textContent || "") + " · no transcript/markers found";
    }

    const tick = () =>
      syncActiveTimeline(els, ui.audio, ui.playhead, durationMs, follow);
    ui.audio.addEventListener("timeupdate", tick, { signal });
    ui.audio.addEventListener("seeked", tick, { signal });
    ui.audio.addEventListener(
      "play",
      () => {
        const loop = () => {
          if (ui.audio.paused) return;
          tick();
          requestAnimationFrame(loop);
        };
        requestAnimationFrame(loop);
      },
      { signal },
    );

    // Live-run polling: while a scenario is running, cues.json grows as
    // events.jsonl is flushed. Re-fetch and re-render when the payload
    // changes so the transcript follows the call in real time.
    const CUES_POLL_MS = 3000;
    let cuesInFlight = false;
    let lastCuesFingerprint = "";
    const cuesFingerprint = (p: CuesPayload): string =>
      JSON.stringify([
        p.markers?.length ?? 0,
        p.cues?.length ?? 0,
        p.tool_events?.length ?? 0,
        p.chat_history?.length ?? 0,
        (p.audio && "duration_ms" in p.audio ? p.audio.duration_ms : 0) ?? 0,
      ]);

    const applyCues = (data: CuesPayload): void => {
      const markers = data.markers || [];
      const tools = data.tool_events || [];
      const newDurationMs =
        data.audio?.duration_ms != null
          ? Number(data.audio.duration_ms)
          : Math.max(
              0,
              ...markers.map((m) => m.end_ms),
              ...tools.map((t) => t.end_ms),
              ...(data.cues || []).map((c) => c.end_ms),
            ) || 1;
      if (newDurationMs !== durationMs) {
        durationMs = newDurationMs;
        mountAudioTimeline(ui.timeline, ui.playhead, markers, durationMs, ui.audio);
      }
      if (data.audio?.file && !ui.audio.src) {
        ui.audio.src = `/runs/${encodeURIComponent(runId)}/${data.audio.file}`;
      }
      if (data.audio?.file && ui.missing) ui.missing.classList.add("hidden");
      const newItems = buildTimelineItems(data.cues || [], markers, tools);
      mountTimelineList(ui.cuesEl, newItems, ui.audio, onUserSeek);
      mountVerifyBar(ui.verify, {
        script: data.script_verify,
        assertV: data.assert_verify,
        counts: data.marker_counts,
        behavior: data.behavior_summary || data.caller?.behavior_summary || null,
        toolSummary: data.tool_summary,
        observeGaps: data.observe_gaps,
      });
    };

    const pollCues = async (): Promise<void> => {
      if (cuesInFlight || document.visibilityState === "hidden") return;
      cuesInFlight = true;
      try {
        const data = await fetchCues(runId);
        const fp = cuesFingerprint(data);
        if (fp !== lastCuesFingerprint) {
          lastCuesFingerprint = fp;
          applyCues(data);
        }
      } catch {
        /* transient — keep last good view */
      } finally {
        cuesInFlight = false;
      }
    };
    lastCuesFingerprint = cuesFingerprint(data);
    const cuesTimer = window.setInterval(() => {
      void pollCues();
    }, CUES_POLL_MS);
    signal.addEventListener("abort", () => window.clearInterval(cuesTimer), {
      once: true,
    });
    document.addEventListener(
      "visibilitychange",
      () => {
        if (document.visibilityState === "visible") void pollCues();
      },
      { signal },
    );
  } catch (e) {
    ui.subtitle.className = "error";
    ui.subtitle.textContent = String(e);
  }
}
