import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { StepRow, TaskRow, useApi } from "../api";
import { DeviceCanvas } from "../components/DeviceCanvas";
import { Card, Skeleton, StatePill, fmtDur, fmtTime } from "../components/bits";
import { Icon } from "../components/icons";

export default function TaskDetail() {
  const { id } = useParams();
  const { data, error, loading } = useApi<{ task: TaskRow; steps: StepRow[] }>(
    `/tasks/${id}`,
    [id]
  );
  const [idx, setIdx] = useState(0);

  if (loading)
    return (
      <div className="space-y-5">
        <Skeleton className="h-5 w-40" />
        <Skeleton className="h-7 w-2/3" />
        <div className="grid gap-6 lg:grid-cols-[360px_1fr]">
          <Skeleton className="h-[520px] rounded-2xl" />
          <Skeleton className="h-[520px] rounded-2xl" />
        </div>
      </div>
    );
  if (error || !data)
    return (
      <div className="flex items-center gap-2 text-rose-400">
        <Icon.x className="h-4 w-4" /> Couldn't load task {id}.
      </div>
    );

  const { task, steps } = data;
  const frames = steps.map((s) => ({
    step: s.idx,
    screenshotUrl: s.screenshot_url,
    coords: s.coords,
    note: s.note,
    rejected: s.rejected,
    action_type: s.action_type,
  }));
  const focused = steps[Math.min(idx, steps.length - 1)];

  return (
    <div className="space-y-5">
      <div className="flex items-center gap-3">
        <Link
          to="/history"
          className="group inline-flex items-center gap-1 text-sm font-medium text-cyan-300 hover:text-cyan-200"
        >
          <Icon.chevronLeft className="h-4 w-4 transition-transform group-hover:-translate-x-0.5" />
          History
        </Link>
        <StatePill state={task.state} />
      </div>

      <div>
        <h1 className="flex items-center gap-2.5 font-display text-xl font-semibold tracking-tight text-zinc-50">
          <span className="text-2xl">{task.emoji}</span>
          <span className="truncate">{task.description}</span>
        </h1>
        <p className="mt-1.5 font-mono text-xs text-zinc-500">
          {task.app_name} · {fmtTime(task.started_at)} ·{" "}
          {fmtDur(task.duration_seconds)} · {task.step_count} steps ·{" "}
          {task.tokens.in + task.tokens.out} tokens
        </p>
      </div>

      {steps.length === 0 ? (
        <Card className="p-6 text-sm text-zinc-500">
          No per-step screenshots were captured for this run.
        </Card>
      ) : (
        <div className="grid gap-6 lg:grid-cols-[360px_1fr]">
          <Card className="p-5">
            <DeviceCanvas frames={frames} focusedIndex={idx} />
            <input
              type="range"
              min={0}
              max={steps.length - 1}
              value={Math.min(idx, steps.length - 1)}
              onChange={(e) => setIdx(Number(e.target.value))}
              className="mt-5 w-full accent-brand-cyan"
            />
            <div className="mt-2 flex justify-between font-mono text-xs text-zinc-500">
              <span>step 1</span>
              <span>step {steps.length}</span>
            </div>
          </Card>

          <Card className="p-5">
            <div className="mb-1 text-[11px] font-medium uppercase tracking-wider text-zinc-500">
              Step {focused?.idx}:{" "}
              <span className="text-cyan-300">{focused?.action_type ?? "—"}</span>
            </div>
            <div className="text-sm text-zinc-300">{focused?.note}</div>
            {focused?.result && (
              <div className="mt-1 text-xs text-zinc-500">{focused.result}</div>
            )}
            <ol className="mt-4 max-h-[52vh] space-y-0.5 overflow-auto pr-1">
              {steps.map((s, i) => (
                <li key={s.idx}>
                  <button
                    onClick={() => setIdx(i)}
                    className={
                      "flex w-full items-center gap-3 rounded-lg px-2 py-2 text-left text-sm transition " +
                      (i === idx
                        ? "bg-white/5 ring-1 ring-white/10"
                        : "hover:bg-white/[0.04]")
                    }
                  >
                    <span className="w-6 shrink-0 font-mono text-xs text-zinc-500">
                      {s.idx}
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
      )}
    </div>
  );
}
