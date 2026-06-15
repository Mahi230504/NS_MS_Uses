import { useState } from "react";
import { Link } from "react-router-dom";
import { AppStat, TaskRow, useApi } from "../api";
import { Card, Empty, SkeletonRows, StatePill, fmtDur, fmtTime } from "../components/bits";
import { Stagger, StaggerItem } from "../components/motion";
import { Icon } from "../components/icons";

export default function History() {
  const apps = useApi<{ apps: AppStat[] }>("/analytics/apps");
  const [pkg, setPkg] = useState<string | null>(null);
  const q = pkg ? `&app=${encodeURIComponent(pkg)}` : "";
  const tasks = useApi<{ items: TaskRow[] }>(`/tasks?limit=60${q}`, [pkg]);

  return (
    <div className="space-y-5">
      <p className="text-sm text-zinc-500">
        Every task the assistant has run — filter by app, tap any row to replay
        it step by step.
      </p>

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
              <span className="mr-1">{a.emoji}</span>
              {a.app_name}
            </Chip>
          ))}
      </div>

      <Card className="p-2">
        {tasks.loading ? (
          <div className="p-3">
            <SkeletonRows rows={8} />
          </div>
        ) : tasks.data && tasks.data.items.length ? (
          <Stagger className="divide-y divide-white/5" gap={0.03}>
            {tasks.data.items.map((t) => (
              <StaggerItem key={t.id}>
                <Link
                  to={`/tasks/${t.id}`}
                  className="flex items-center gap-3 rounded-xl px-3 py-3 transition hover:bg-white/[0.04]"
                >
                  <span className="grid h-10 w-10 shrink-0 place-items-center rounded-xl bg-white/5 text-lg ring-1 ring-white/5">
                    {t.emoji}
                  </span>
                  <span className="min-w-0 flex-1">
                    <span className="block truncate text-sm text-zinc-200">
                      {t.description}
                    </span>
                    <span className="mt-0.5 block truncate font-mono text-[11px] text-zinc-500">
                      {t.app_name} · {fmtTime(t.started_at)} ·{" "}
                      {fmtDur(t.duration_seconds)} · {t.step_count} steps
                    </span>
                  </span>
                  <StatePill state={t.state} />
                  <Icon.chevronRight className="hidden h-4 w-4 text-zinc-600 sm:block" />
                </Link>
              </StaggerItem>
            ))}
          </Stagger>
        ) : (
          <Empty icon={<Icon.history className="h-6 w-6" />}>
            No tasks yet for this filter.
          </Empty>
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
        "rounded-full px-3.5 py-1.5 text-sm font-medium ring-1 transition " +
        (active
          ? "bg-brand-soft text-cyan-200 ring-brand-cyan/30"
          : "bg-white/5 text-zinc-400 ring-white/10 hover:text-zinc-200")
      }
    >
      {children}
    </button>
  );
}
