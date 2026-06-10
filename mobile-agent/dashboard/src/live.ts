import { useEffect } from "react";
import { create } from "zustand";
import { withToken } from "./api";

export interface LiveStep {
  step: number;
  phase: string; // "pending" | "final"
  action_type: string | null;
  note: string;
  coords: {
    tap?: { x: number; y: number };
    swipe?: { x1: number; y1: number; x2: number; y2: number };
    text?: string;
  };
  result: string | null;
  rejected: boolean;
  screenshot_url: string | null;
}

interface LiveState {
  connected: boolean;
  taskId: number | null;
  state: string;
  description: string | null;
  summary: string | null;
  steps: Map<number, LiveStep>;
  approval: { reason: string; note: string } | null;
  setConnected: (b: boolean) => void;
  apply: (ev: any) => void;
}

export const useLive = create<LiveState>((set) => ({
  connected: false,
  taskId: null,
  state: "idle",
  description: null,
  summary: null,
  steps: new Map(),
  approval: null,
  setConnected: (b) => set({ connected: b }),
  apply: (ev) =>
    set((s) => {
      if (ev.type === "step") {
        const newTask = ev.task_id != null && ev.task_id !== s.taskId;
        const steps = newTask ? new Map<number, LiveStep>() : new Map(s.steps);
        steps.set(ev.step, {
          step: ev.step,
          phase: ev.phase,
          action_type: ev.action_type ?? null,
          note: ev.note ?? "",
          coords: ev.coords ?? {},
          result: ev.result ?? null,
          rejected: !!ev.rejected,
          screenshot_url: ev.screenshot_url ?? null,
        });
        return {
          steps,
          taskId: ev.task_id ?? s.taskId,
          state: ev.state || s.state,
          approval: null,
        };
      }
      if (ev.type === "state") {
        const newTask = ev.task_id != null && ev.task_id !== s.taskId;
        return {
          taskId: ev.task_id ?? s.taskId,
          state: ev.state || s.state,
          description: ev.description ?? s.description,
          summary: ev.final_summary ?? s.summary,
          steps: newTask ? new Map<number, LiveStep>() : s.steps,
          approval: ev.state && ev.state !== "running" ? null : s.approval,
        };
      }
      if (ev.type === "approval") {
        return { approval: { reason: ev.reason || "", note: ev.note || "" } };
      }
      return {};
    }),
}));

/** Open the SSE stream once and feed events into the live store. */
export function useEventStream() {
  const apply = useLive((s) => s.apply);
  const setConnected = useLive((s) => s.setConnected);
  useEffect(() => {
    const es = new EventSource(withToken("/events"));
    const onMsg = (e: MessageEvent) => {
      try {
        apply(JSON.parse(e.data));
      } catch {
        /* ignore malformed frame */
      }
    };
    const names = ["step", "state", "approval", "lag", "keepalive", "message"];
    names.forEach((n) => es.addEventListener(n, onMsg as EventListener));
    es.onopen = () => setConnected(true);
    es.onerror = () => setConnected(false);
    return () => es.close();
  }, [apply, setConnected]);
}

export const orderedSteps = (steps: Map<number, LiveStep>): LiveStep[] =>
  Array.from(steps.values()).sort((a, b) => a.step - b.step);
