import { useState } from "react";
import { Link } from "react-router-dom";
import { AppStat, TaskRow, useApi } from "../api";
import { Card, Empty, Spinner, StatePill, fmtDur, fmtTime } from "../components/bits";

export default function History() {
  const apps = useApi<{ apps: AppStat[] }>("/analytics/apps");
  const [pkg, setPkg] = useState<string | null>(null);
  const q = pkg ? `&app=${encodeURIComponent(pkg)}` : "";
  const tasks = useApi<{ items: TaskRow[] }>(`/tasks?limit=60${q}`, [pkg]);

  return (
    <div className="space-y-5">
      <h1 className="text-xl font-semibold text-zinc-100">History</h1>

      <div className="flex flex-wrap gap-2">
        <Chip active={pkg === null} onClick={() => setPkg(null)}>
          All
        </Chip>
        {(apps.data?.apps ?? [])
          .filter((a) => a.launch_package)
          .map((a) => (
            <Chip
              key={a.launch_package}
              active={pkg === a.launch_package}
              onClick={() => setPkg(a.launch_package)}
            >
              {a.emoji} {a.app_name}
            </Chip>
          ))}
      </div>

      <Card className="p-2">
        {tasks.loading ? (
          <div className="p-4">
            <Spinner />
          </div>
        ) : tasks.data && tasks.data.items.length ? (
          <ul className="divide-y divide-white/5">
            {tasks.data.items.map((t) => (
              <li key={t.id}>
                <Link
                  to={`/tasks/${t.id}`}
                  className="flex items-center gap-3 px-3 py-2.5 hover:bg-white/5"
                >
                  <span className="text-lg">{t.emoji}</span>
                  <span className="min-w-0 flex-1">
                    <span className="block truncate text-sm text-zinc-200">
                      {t.description}
                    </span>
                    <span className="text-xs text-zinc-500">
                      {t.app_name} · {fmtTime(t.started_at)} · {fmtDur(t.duration_seconds)}{" "}
                      · {t.step_count} steps
                    </span>
                  </span>
                  <StatePill state={t.state} />
                </Link>
              </li>
            ))}
          </ul>
        ) : (
          <Empty>No tasks yet for this filter.</Empty>
        )}
      </Card>
    </div>
  );
}

function Chip({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      onClick={onClick}
      className={
        "rounded-full px-3 py-1 text-sm ring-1 " +
        (active
          ? "bg-cyan-500/15 text-cyan-300 ring-cyan-400/30"
          : "bg-white/5 text-zinc-400 ring-white/10 hover:text-zinc-200")
      }
    >
      {children}
    </button>
  );
}
