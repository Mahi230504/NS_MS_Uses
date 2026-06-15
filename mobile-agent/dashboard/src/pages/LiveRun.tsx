import { useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { apiPost } from "../api";
import { DeviceCanvas } from "../components/DeviceCanvas";
import { Card, Empty, StatePill } from "../components/bits";
import { Icon } from "../components/icons";
import { orderedSteps, useLive } from "../live";

export default function LiveRun() {
  const taskId = useLive((s) => s.taskId);
  const state = useLive((s) => s.state);
  const description = useLive((s) => s.description);
  const summary = useLive((s) => s.summary);
  const approval = useLive((s) => s.approval);
  const clearApproval = useLive((s) => s.clearApproval);
  const steps = useLive((s) => orderedSteps(s.steps));
  const [deciding, setDeciding] = useState(false);

  const decide = async (decision: "approve" | "deny") => {
    setDeciding(true);
    try {
      await apiPost("/approval", { decision });
      clearApproval();
    } catch {
      /* leave the banner up so the user can retry (or use Telegram) */
    } finally {
      setDeciding(false);
    }
  };

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
      <Empty icon={<Icon.live className="h-6 w-6" />}>
        No task running right now. Start one from Telegram or voice — it appears
        here live, with the agent's taps drawn on each screen.
      </Empty>
    );
  }

  return (
    <div className="grid gap-6 lg:grid-cols-[360px_1fr]">
      <Card className="p-5">
        <DeviceCanvas frames={frames} focusedIndex={focusedIndex} />
        {steps.length > 1 && (
          <div className="mt-5">
            <input
              type="range"
              min={0}
              max={lastIndex}
              value={focusedIndex}
              onChange={(e) => setManual(Number(e.target.value))}
              className="w-full accent-brand-cyan"
            />
            <div className="mt-2 flex items-center justify-between text-xs text-zinc-500">
              <span className="font-mono">step 1</span>
              <button
                onClick={() => setManual(null)}
                className={
                  "inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 font-medium transition " +
                  (following
                    ? "text-cyan-300"
                    : "bg-brand-cyan/10 text-cyan-300 ring-1 ring-brand-cyan/30 hover:bg-brand-cyan/20")
                }
              >
                <span className="relative flex h-1.5 w-1.5">
                  {following && (
                    <span className="absolute inline-flex h-full w-full rounded-full bg-brand-cyan animate-ping2" />
                  )}
                  <span className="relative inline-flex h-1.5 w-1.5 rounded-full bg-brand-cyan" />
                </span>
                {following ? "following live" : "jump to live"}
              </button>
              <span className="font-mono">step {steps.length}</span>
            </div>
          </div>
        )}
      </Card>

      <div className="space-y-4">
        <Card className="p-5">
          <div className="flex items-center justify-between gap-3">
            <div className="min-w-0">
              <div className="text-[11px] font-medium uppercase tracking-wider text-zinc-500">
                Current task
              </div>
              <div className="mt-1.5 truncate text-base text-zinc-100">
                {description || "—"}
              </div>
            </div>
            <StatePill state={state} />
          </div>
          {summary && state === "done" && (
            <div className="mt-4 flex items-start gap-2.5 rounded-xl bg-emerald-500/10 p-3.5 text-sm text-emerald-200 ring-1 ring-emerald-400/20">
              <Icon.check className="mt-0.5 h-4 w-4 shrink-0" />
              <span>{summary}</span>
            </div>
          )}
        </Card>

        <AnimatePresence>
          {approval && (
            <motion.div
              initial={{ opacity: 0, scale: 0.97, y: -6 }}
              animate={{ opacity: 1, scale: 1, y: 0 }}
              exit={{ opacity: 0, scale: 0.97 }}
              transition={{ type: "spring", stiffness: 360, damping: 28 }}
            >
              <Card glow="amber" className="animate-glow-pulse p-5">
                <div className="flex items-center gap-2 font-medium text-amber-300">
                  <span className="relative flex h-2 w-2">
                    <span className="absolute inline-flex h-full w-full rounded-full bg-amber-400 animate-ping2" />
                    <span className="relative inline-flex h-2 w-2 rounded-full bg-amber-400" />
                  </span>
                  Approval needed
                </div>
                <div className="mt-2 text-sm text-zinc-300">{approval.reason}</div>
                <div className="mt-4 flex gap-3">
                  <button
                    disabled={deciding}
                    onClick={() => decide("approve")}
                    className="flex flex-1 items-center justify-center gap-1.5 rounded-xl bg-emerald-500 px-3 py-2.5 text-sm font-semibold text-ink transition hover:bg-emerald-400 disabled:opacity-50"
                  >
                    <Icon.check className="h-4 w-4" />
                    Approve
                  </button>
                  <button
                    disabled={deciding}
                    onClick={() => decide("deny")}
                    className="flex flex-1 items-center justify-center gap-1.5 rounded-xl bg-rose-500/20 px-3 py-2.5 text-sm font-semibold text-rose-200 ring-1 ring-rose-400/40 transition hover:bg-rose-500/30 disabled:opacity-50"
                  >
                    <Icon.x className="h-4 w-4" />
                    Deny
                  </button>
                </div>
                <div className="mt-2.5 text-center text-xs text-zinc-500">
                  You can also approve in Telegram.
                </div>
              </Card>
            </motion.div>
          )}
        </AnimatePresence>

        <Card className="p-5">
          <div className="mb-3 text-[11px] font-medium uppercase tracking-wider text-zinc-500">
            Steps
          </div>
          <ol className="space-y-0.5">
            {steps.map((s, i) => (
              <li key={s.step}>
                <button
                  onClick={() => setManual(i)}
                  className={
                    "flex w-full items-center gap-3 rounded-lg px-2 py-2 text-left text-sm transition " +
                    (i === focusedIndex
                      ? "bg-white/5 ring-1 ring-white/10"
                      : "hover:bg-white/[0.04]")
                  }
                >
                  <span className="w-6 shrink-0 font-mono text-xs text-zinc-500">
                    {s.step}
                  </span>
                  <span
                    className={
                      "w-20 shrink-0 truncate font-mono text-xs " +
                      (s.rejected ? "text-rose-400" : "text-cyan-300")
                    }
                  >
                    {s.action_type ?? "—"}
                  </span>
                  <span className="truncate text-zinc-400">
                    {s.note || s.result}
                  </span>
                </button>
              </li>
            ))}
          </ol>
        </Card>
      </div>
    </div>
  );
}
