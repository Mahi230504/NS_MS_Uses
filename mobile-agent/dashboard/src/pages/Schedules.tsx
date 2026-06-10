import { ScheduleRow, useApi } from "../api";
import { Card, Empty, Spinner, fmtTime } from "../components/bits";

const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

function phrase(s: ScheduleRow): string {
  const hh = String(Math.floor(s.at_minute / 60)).padStart(2, "0");
  const mm = String(s.at_minute % 60).padStart(2, "0");
  const t = `${hh}:${mm}`;
  if (s.freq === "daily") return `every day at ${t}`;
  if (s.freq === "weekly")
    return `every ${s.weekday != null ? DAYS[s.weekday] : "?"} at ${t}`;
  if (s.freq === "monthly") return `day ${s.day_of_month} each month at ${t}`;
  return `once at ${t}`;
}

export default function Schedules() {
  const { data, loading } = useApi<{ items: ScheduleRow[] }>("/schedules");
  return (
    <div className="space-y-5">
      <h1 className="text-xl font-semibold text-zinc-100">Schedules</h1>
      {loading ? (
        <Spinner />
      ) : data && data.items.length ? (
        <div className="space-y-3">
          {data.items.map((s) => (
            <Card key={s.id} className="flex items-center gap-4 p-4">
              <span className="text-lg">{s.emoji}</span>
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2">
                  <span className="font-medium text-zinc-100">{s.name}</span>
                  {s.pay_automatically && (
                    <span className="rounded-full bg-amber-500/15 px-2 py-0.5 text-[11px] text-amber-300">
                      auto-pay
                    </span>
                  )}
                  {!s.enabled && (
                    <span className="rounded-full bg-zinc-500/15 px-2 py-0.5 text-[11px] text-zinc-400">
                      off
                    </span>
                  )}
                </div>
                <div className="text-sm text-zinc-400">{phrase(s)}</div>
                <div className="truncate text-xs text-zinc-500">{s.description}</div>
              </div>
              <div className="text-right text-xs text-zinc-500">
                <div>next</div>
                <div className="text-zinc-300">{fmtTime(s.next_run_at)}</div>
                <code className="mt-1 block rounded bg-white/5 px-1.5 py-0.5">
                  /unschedule {s.id}
                </code>
              </div>
            </Card>
          ))}
        </div>
      ) : (
        <Empty>
          No schedules. Say “order milk on blinkit every day at 9am”, or use
          /schedule.
        </Empty>
      )}
    </div>
  );
}
