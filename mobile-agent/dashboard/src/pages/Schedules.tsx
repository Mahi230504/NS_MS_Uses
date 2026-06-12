import { useState } from "react";
import { ScheduleRow, useApi } from "../api";
import { Spinner } from "../components/bits";

// Sunday-first, like Google Calendar's default month view.
const DOW = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const MONTHS = [
  "January", "February", "March", "April", "May", "June",
  "July", "August", "September", "October", "November", "December",
];

const hhmm = (m: number) =>
  `${String(Math.floor(m / 60)).padStart(2, "0")}:${String(m % 60).padStart(2, "0")}`;

// Backend stores weekday as 0=Mon..6=Sun; convert a JS Date to that.
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

// A palette so different schedules get distinct event colors (Google-ish).
const EVENT_COLORS = [
  "bg-cyan-500/20 text-cyan-200 ring-cyan-400/30",
  "bg-emerald-500/20 text-emerald-200 ring-emerald-400/30",
  "bg-violet-500/20 text-violet-200 ring-violet-400/30",
  "bg-amber-500/20 text-amber-200 ring-amber-400/30",
  "bg-rose-500/20 text-rose-200 ring-rose-400/30",
];

export default function Schedules() {
  const { data, loading } = useApi<{ items: ScheduleRow[] }>("/schedules");
  const items = data?.items ?? [];
  const colorOf = (id: number) => EVENT_COLORS[id % EVENT_COLORS.length];

  const [view, setView] = useState(() => {
    const n = new Date();
    return { y: n.getFullYear(), m: n.getMonth() };
  });
  const shift = (delta: number) => {
    const m = view.m + delta;
    setView({ y: view.y + Math.floor(m / 12), m: ((m % 12) + 12) % 12 });
  };
  const goToday = () => {
    const n = new Date();
    setView({ y: n.getFullYear(), m: n.getMonth() });
  };

  const first = new Date(view.y, view.m, 1);
  const startPad = first.getDay(); // Sunday-first
  const daysInMonth = new Date(view.y, view.m + 1, 0).getDate();
  const cells: (Date | null)[] = [
    ...Array(startPad).fill(null),
    ...Array.from({ length: daysInMonth }, (_, i) => new Date(view.y, view.m, i + 1)),
  ];
  while (cells.length % 7 !== 0) cells.push(null);
  const weeks = Math.ceil(cells.length / 7);
  const todayStr = new Date().toDateString();

  return (
    <div className="flex h-full flex-col space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold text-zinc-100">Schedules</h1>
          {items.length === 0 && (
            <p className="text-sm text-zinc-500">
              No schedules yet — say "order milk on blinkit every day at 9am" or use
              /schedule, and it'll appear here.
            </p>
          )}
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={goToday}
            className="rounded-lg bg-white/5 px-3 py-1 text-sm text-zinc-300 hover:bg-white/10"
          >
            Today
          </button>
          <div className="flex items-center gap-1">
            <button onClick={() => shift(-1)} className="rounded-lg bg-white/5 px-2.5 py-1 text-zinc-300 hover:bg-white/10">‹</button>
            <span className="min-w-[10rem] text-center text-sm font-medium text-zinc-200">
              {MONTHS[view.m]} {view.y}
            </span>
            <button onClick={() => shift(1)} className="rounded-lg bg-white/5 px-2.5 py-1 text-zinc-300 hover:bg-white/10">›</button>
          </div>
        </div>
      </div>

      {loading ? (
        <Spinner />
      ) : (
        <div className="flex flex-1 flex-col overflow-hidden rounded-xl ring-1 ring-white/10">
          {/* weekday header */}
          <div className="grid grid-cols-7 border-b border-white/10 bg-white/[0.03]">
            {DOW.map((d) => (
              <div key={d} className="px-2 py-2 text-center text-[11px] font-medium uppercase tracking-wide text-zinc-500">
                {d}
              </div>
            ))}
          </div>
          {/* day grid */}
          <div
            className="grid flex-1 grid-cols-7"
            style={{ gridTemplateRows: `repeat(${weeks}, minmax(96px, 1fr))` }}
          >
            {cells.map((d, i) => {
              if (!d)
                return <div key={i} className="border-b border-r border-white/5 bg-white/[0.01]" />;
              const fires = items.filter((s) => firesOn(s, d));
              const isToday = d.toDateString() === todayStr;
              return (
                <div
                  key={i}
                  className="min-w-0 border-b border-r border-white/5 p-1.5 hover:bg-white/[0.02]"
                >
                  <div className="flex justify-end">
                    <span
                      className={
                        "grid h-6 w-6 place-items-center rounded-full text-xs " +
                        (isToday ? "bg-cyan-500 font-semibold text-black" : "text-zinc-400")
                      }
                    >
                      {d.getDate()}
                    </span>
                  </div>
                  <div className="mt-1 space-y-1">
                    {fires.slice(0, 3).map((s) => (
                      <div
                        key={s.id}
                        title={`${s.name} — ${hhmm(s.at_minute)}${s.pay_automatically ? " (auto-pay)" : ""}`}
                        className={"truncate rounded px-1.5 py-0.5 text-[10px] ring-1 " + colorOf(s.id)}
                      >
                        {hhmm(s.at_minute)} {s.action_kind !== "device" ? "✉" : s.emoji}{" "}
                        {s.name}
                      </div>
                    ))}
                    {fires.length > 3 && (
                      <div className="px-1 text-[10px] text-zinc-500">+{fires.length - 3} more</div>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}
