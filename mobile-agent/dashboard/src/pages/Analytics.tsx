import { AppStat, useApi } from "../api";
import { Card, Empty, Spinner, fmtTime } from "../components/bits";

export default function Analytics() {
  const { data, loading } = useApi<{ apps: AppStat[] }>("/analytics/apps");
  const apps = data?.apps ?? [];
  const max = Math.max(1, ...apps.map((a) => a.run_count));

  return (
    <div className="space-y-5">
      <h1 className="text-xl font-semibold text-zinc-100">Your apps</h1>
      <p className="text-sm text-zinc-500">
        What you actually use the assistant for, most-used first.
      </p>
      {loading ? (
        <Spinner />
      ) : apps.length ? (
        <Card className="p-5">
          <div className="space-y-4">
            {apps.map((a) => (
              <div key={a.launch_package ?? a.app_name}>
                <div className="mb-1 flex items-center justify-between text-sm">
                  <span className="text-zinc-200">
                    {a.emoji} {a.app_name}
                  </span>
                  <span className="text-xs text-zinc-500">
                    {a.run_count} runs · {Math.round(a.success_rate * 100)}% ok ·
                    avg {a.avg_steps} steps · {fmtTime(a.last_used_at)}
                  </span>
                </div>
                <div className="h-2 overflow-hidden rounded-full bg-white/5">
                  <div
                    className="h-full rounded-full bg-gradient-to-r from-cyan-500 to-emerald-400"
                    style={{ width: `${(a.run_count / max) * 100}%` }}
                  />
                </div>
              </div>
            ))}
          </div>
        </Card>
      ) : (
        <Empty>No usage yet.</Empty>
      )}
    </div>
  );
}
