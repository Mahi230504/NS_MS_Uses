import { useState } from "react";
import { ScheduleRow, useApi } from "../api";
import { Card, Empty, Spinner, fmtTime } from "../components/bits";

const DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const MONTHS = [
  "January", "February", "March", "April", "May", "June",
  "July", "August", "September", "October", "November", "December",
];

const hhmm = (atMinute: number) =>
  `${String(Math.floor(atMinute / 60)).padStart(2, "0")}:${String(atMinute % 60).padStart(2, "0")}`;

// Monday-based weekday for a JS Date (backend stores 0=Mon..6=Sun).
const monWeekday = (d: Date) => (d.getDay() + 6) % 7;

function firesOn(s: ScheduleRow, d: Date): boolean {
  if (!s.enabled) return false;
  if (s.freq === "daily") return true;
  if (s.freq === "weekly") return s.weekday === monWeekday(d);
  if (s.freq === "monthly") {
    const dim = new Date(d.getFullYear(), d.getMonth() + 1, 0).getDate();
    return d.getDate() === Math.min(s.day_of_month ?? 1, dim);
  }
  if (s.freq === "once") {
    try {
      return new Date(s.next_run_at).toDateString() === d.toDateString();
    } catch {
      return false;
    }
  }
  return false;
}

export default function Schedules() {
  const { data, loading } = useApi<{ items: ScheduleRow[] }>("/schedules");
  const items = data?.items ?? [];
  const [view, setView] = useState(() => {
    const n = new Date();
    return { y: n.getFullYear(), m: n.getMonth() };
  });

  const first = new Date(view.y, view.m, 1);
  const startPad = monWeekday(first);
  const daysInMonth = new Date(view.y, view.m + 1, 0).getDate();
  const cells: (Date | null)[] = [
    ...Array(startPad).fill(null),
    ...Array.from({ length: daysInMonth }, (_, i) => new Date(view.y, view.m, i + 1)),
  ];
  while (cells.length % 7 !== 0) cells.push(null);
  const todayStr = new Date().toDateString();

  const shift = (delta: number) => {
    const m = view.m + delta;
    setView({ y: view.y + Math.floor(m / 12), m: ((m % 12) + 12) % 12 });
  };

  return (
    <div className="space-y-5">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold text-zinc-100">Schedules</h1>
        <div className="flex items-center gap-2">
          <button onClick={() => shift(-1)} className="rounded-lg bg-white/5 px-2.5 py-1 text-zinc-300 hover:bg-white/10">‹</button>
          <span className="min-w-[9rem] text-center text-sm text-zinc-300">
            {MONTHS[view.m]} {view.y}
          </span>
          <button onClick={() => shift(1)} className="rounded-lg bg-white/5 px-2.5 py-1 text-zinc-300 hover:bg-white/10">›</button>
        </div>
      </div>

      {loading ? (
        <Spinner />
      ) : items.length === 0 ? (
        <Empty>
          No schedules. Say "order milk on blinkit every day at 9am", or use
          /schedule.
        </Empty>
      ) : (
        <>
          <Card className="p-3">
            <div className="grid grid-cols-7 gap-1 text-center text-[11px] uppercase tracking-wide text-zinc-500">
              {DOW.map((d) => (
                <div key={d} className="py-1">{d}</div>
              ))}
            </div>
            <div className="grid grid-cols-7 gap-1">
              {cells.map((d, i) => {
                if (!d) return <div key={i} className="min-h-[84px] rounded-lg" />;
                const fires = items.filter((s) => firesOn(s, d));
                const isToday = d.toDateString() === todayStr;
                return (
                  <div
                    key={i}
                    className={
                      "min-h-[84px] rounded-lg p-1.5 ring-1 " +
                      (isToday
                        ? "bg-cyan-500/10 ring-cyan-400/40"
                        : "bg-white/[0.02] ring-white/5")
                    }
                  >
                    <div className={"text-xs " + (isToday ? "text-cyan-300" : "text-zinc-500")}>
                      {d.getDate()}
                    </div>
                    <div className="mt-1 space-y-1">
                      {fires.slice(0, 3).map((s) => (
                        <div
                          key={s.id}
                          title={`${s.name} — ${hhmm(s.at_minute)}${s.pay_automatically ? " (auto-pay)" : ""}`}
                          className="truncate rounded bg-cyan-500/15 px-1 py-0.5 text-[10px] text-cyan-200"
                        >
                          {s.emoji} {hhmm(s.at_minute)} {s.name}
                        </div>
                      ))}
                      {fires.length > 3 && (
                        <div className="text-[10px] text-zinc-500">+{fires.length - 3} more</div>
                      )}
                    </div>
                  </div>
                );
              })}
            </div>
          </Card>

          <Card className="p-4">
            <div className="mb-2 text-xs uppercase tracking-wide text-zinc-500">
              All schedules
            </div>
            <ul className="divide-y divide-white/5">
              {items.map((s) => (
                <li key={s.id} className="flex items-center gap-3 py-2 text-sm">
                  <span className="text-lg">{s.emoji}</span>
                  <span className="flex-1 truncate">
                    <span className="text-zinc-200">{s.name}</span>
                    {s.pay_automatically && (
                      <span className="ml-2 rounded-full bg-amber-500/15 px-1.5 py-0.5 text-[10px] text-amber-300">auto-pay</span>
                    )}
                    {!s.enabled && (
                      <span className="ml-2 rounded-full bg-zinc-500/15 px-1.5 py-0.5 text-[10px] text-zinc-400">off</span>
                    )}
                  </span>
                  <span className="text-xs text-zinc-500">
                    next {fmtTime(s.next_run_at)} · <code className="rounded bg-white/5 px-1">/unschedule {s.id}</code>
                  </span>
                </li>
              ))}
            </ul>
          </Card>
        </>
      )}
    </div>
  );
}
