import { useState } from "react";
import { DeviceCanvas } from "../components/DeviceCanvas";
import { Card, Empty, StatePill } from "../components/bits";
import { orderedSteps, useLive } from "../live";

export default function LiveRun() {
  const taskId = useLive((s) => s.taskId);
  const state = useLive((s) => s.state);
  const description = useLive((s) => s.description);
  const summary = useLive((s) => s.summary);
  const approval = useLive((s) => s.approval);
  const steps = useLive((s) => orderedSteps(s.steps));

  // Auto-follow the latest step unless the user scrubs back.
  const [manual, setManual] = useState<number | null>(null);
  const lastIndex = Math.max(0, steps.length - 1);
  const focusedIndex = manual === null ? lastIndex : Math.min(manual, lastIndex);
  const following = manual === null;

  const frames = steps.map((s) => ({
    step: s.step,
    screenshotUrl: s.screenshot_url,
    coords: s.coords,
    note: s.note,
    rejected: s.rejected,
    action_type: s.action_type,
  }));

  if (!taskId && steps.length === 0) {
    return (
      <Empty>
        No task running right now. Start one from Telegram or voice — it appears
        here live, with the agent's taps drawn on each screen.
      </Empty>
    );
  }

  return (
    <div className="grid gap-6 lg:grid-cols-[340px_1fr]">
      <Card className="p-5">
        <DeviceCanvas frames={frames} focusedIndex={focusedIndex} />
        {steps.length > 1 && (
          <div className="mt-4">
            <input
              type="range"
              min={0}
              max={lastIndex}
              value={focusedIndex}
              onChange={(e) => setManual(Number(e.target.value))}
              className="w-full accent-cyan-400"
            />
            <div className="mt-1 flex justify-between text-xs text-zinc-500">
              <span>step 1</span>
              <button
                onClick={() => setManual(null)}
                className={
                  "rounded px-2 py-0.5 " +
                  (following
                    ? "text-cyan-300"
                    : "bg-cyan-500/10 text-cyan-300 hover:bg-cyan-500/20")
                }
              >
                {following ? "● following live" : "jump to live"}
              </button>
              <span>step {steps.length}</span>
            </div>
          </div>
        )}
      </Card>

      <div className="space-y-4">
        <Card className="p-5">
          <div className="flex items-center justify-between gap-3">
            <div className="min-w-0">
              <div className="text-xs uppercase tracking-wide text-zinc-500">
                Current task
              </div>
              <div className="mt-1 truncate text-zinc-100">
                {description || "—"}
              </div>
            </div>
            <StatePill state={state} />
          </div>
          {summary && state === "done" && (
            <div className="mt-3 rounded-lg bg-emerald-500/10 p-3 text-sm text-emerald-200">
              {summary}
            </div>
          )}
        </Card>

        {approval && (
          <Card className="p-5 ring-amber-400/30">
            <div className="flex items-center gap-2 text-amber-300">
              <span className="h-2 w-2 rounded-full bg-amber-400" />
              Approval pending — respond in Telegram
            </div>
            <div className="mt-2 text-sm text-zinc-300">{approval.reason}</div>
          </Card>
        )}

        <Card className="p-5">
          <div className="mb-2 text-xs uppercase tracking-wide text-zinc-500">
            Steps
          </div>
          <ol className="space-y-1">
            {steps.map((s, i) => (
              <li key={s.step}>
                <button
                  onClick={() => setManual(i)}
                  className={
                    "flex w-full items-center gap-3 rounded-lg px-2 py-1.5 text-left text-sm " +
                    (i === focusedIndex
                      ? "bg-white/5 ring-1 ring-white/10"
                      : "hover:bg-white/5")
                  }
                >
                  <span className="w-6 font-mono text-xs text-zinc-500">
                    {s.step}
                  </span>
                  <span
                    className={
                      "w-16 shrink-0 text-xs " +
                      (s.rejected ? "text-red-400" : "text-cyan-300")
                    }
                  >
                    {s.action_type ?? "—"}
                  </span>
                  <span className="truncate text-zinc-400">{s.note || s.result}</span>
                </button>
              </li>
            ))}
          </ol>
        </Card>
      </div>
    </div>
  );
}
