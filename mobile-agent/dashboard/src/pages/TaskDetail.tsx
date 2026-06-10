import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { StepRow, TaskRow, useApi } from "../api";
import { DeviceCanvas } from "../components/DeviceCanvas";
import { Card, Spinner, StatePill, fmtDur, fmtTime } from "../components/bits";

export default function TaskDetail() {
  const { id } = useParams();
  const { data, error, loading } = useApi<{ task: TaskRow; steps: StepRow[] }>(
    `/tasks/${id}`,
    [id]
  );
  const [idx, setIdx] = useState(0);

  if (loading) return <Spinner />;
  if (error || !data)
    return <div className="text-red-400">Couldn't load task {id}.</div>;

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
        <Link to="/history" className="text-sm text-cyan-300">
          ← History
        </Link>
        <StatePill state={task.state} />
      </div>

      <div>
        <h1 className="truncate text-lg font-semibold text-zinc-100">
          {task.emoji} {task.description}
        </h1>
        <p className="text-sm text-zinc-500">
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
        <div className="grid gap-6 lg:grid-cols-[340px_1fr]">
          <Card className="p-5">
            <DeviceCanvas frames={frames} focusedIndex={idx} />
            <input
              type="range"
              min={0}
              max={steps.length - 1}
              value={Math.min(idx, steps.length - 1)}
              onChange={(e) => setIdx(Number(e.target.value))}
              className="mt-4 w-full accent-cyan-400"
            />
            <div className="mt-1 flex justify-between text-xs text-zinc-500">
              <span>step 1</span>
              <span>step {steps.length}</span>
            </div>
          </Card>

          <Card className="p-5">
            <div className="mb-2 text-xs uppercase tracking-wide text-zinc-500">
              Step {focused?.idx}: {focused?.action_type ?? "—"}
            </div>
            <div className="text-sm text-zinc-300">{focused?.note}</div>
            <div className="mt-1 text-xs text-zinc-500">{focused?.result}</div>
            <ol className="mt-4 max-h-[50vh] space-y-1 overflow-auto">
              {steps.map((s, i) => (
                <li key={s.idx}>
                  <button
                    onClick={() => setIdx(i)}
                    className={
                      "flex w-full items-center gap-3 rounded-lg px-2 py-1.5 text-left text-sm " +
                      (i === idx ? "bg-white/5 ring-1 ring-white/10" : "hover:bg-white/5")
                    }
                  >
                    <span className="w-6 font-mono text-xs text-zinc-500">
                      {s.idx}
                    </span>
                    <span
                      className={
                        "w-16 shrink-0 text-xs " +
                        (s.rejected ? "text-red-400" : "text-cyan-300")
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
