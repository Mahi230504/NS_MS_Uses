import { Link } from "react-router-dom";
import { TaskRow, useApi } from "../api";
import { Card, Spinner, StatePill, Tile, fmtDur, fmtTime } from "../components/bits";

interface Summary {
  total_tasks: number;
  success_rate: number;
  top_app: { app_name: string; emoji: string } | null;
}

export default function Overview() {
  const s = useApi<Summary>("/analytics/summary");
  const recent = useApi<{ items: TaskRow[] }>("/tasks?limit=8");

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold text-zinc-100">Welcome back</h1>
        <p className="text-sm text-zinc-500">
          Everything your assistant has done — and what it's doing right now.
        </p>
      </div>

      <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
        <Tile label="Tasks run" value={s.data?.total_tasks ?? "—"} />
        <Tile
          label="Success rate"
          value={s.data ? `${Math.round(s.data.success_rate * 100)}%` : "—"}
        />
        <Tile
          label="Top app"
          value={
            s.data?.top_app
              ? `${s.data.top_app.emoji} ${s.data.top_app.app_name}`
              : "—"
          }
        />
        <Tile label="Live" value={<Link className="text-cyan-300" to="/live">Open →</Link>} />
      </div>

      <Card className="p-5">
        <div className="mb-3 flex items-center justify-between">
          <div className="text-sm text-zinc-400">Recent activity</div>
          <Link to="/history" className="text-xs text-cyan-300">
            All history →
          </Link>
        </div>
        {recent.loading ? (
          <Spinner />
        ) : recent.data && recent.data.items.length ? (
          <ul className="divide-y divide-white/5">
            {recent.data.items.map((t) => (
              <li key={t.id}>
                <Link
                  to={`/tasks/${t.id}`}
                  className="flex items-center gap-3 py-2.5 hover:opacity-80"
                >
                  <span className="text-lg">{t.emoji}</span>
                  <span className="min-w-0 flex-1 truncate text-sm text-zinc-200">
                    {t.description}
                  </span>
                  <span className="hidden text-xs text-zinc-500 sm:block">
                    {fmtDur(t.duration_seconds)} · {fmtTime(t.started_at)}
                  </span>
                  <StatePill state={t.state} />
                </Link>
              </li>
            ))}
          </ul>
        ) : (
          <div className="text-sm text-zinc-500">
            Nothing yet — run a task from Telegram or voice and it shows up here.
          </div>
        )}
      </Card>
    </div>
  );
}
