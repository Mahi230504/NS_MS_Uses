import { Link } from "react-router-dom";
import { TaskRow, useApi } from "../api";
import {
  Card,
  GradientText,
  SkeletonRows,
  StatePill,
  Tile,
  fmtDur,
  fmtTime,
} from "../components/bits";
import { CountUp, Reveal, Stagger, StaggerItem } from "../components/motion";
import { Icon } from "../components/icons";

interface Summary {
  total_tasks: number;
  success_rate: number;
  top_app: { app_name: string; emoji: string } | null;
}

export default function Overview() {
  const s = useApi<Summary>("/analytics/summary");
  const recent = useApi<{ items: TaskRow[] }>("/tasks?limit=8");

  return (
    <div className="space-y-7">
      {/* hero */}
      <Reveal>
        <div className="relative overflow-hidden rounded-3xl bg-panel/60 p-7 ring-1 ring-white/10 backdrop-blur-xl">
          <div className="pointer-events-none absolute -right-16 -top-20 h-56 w-56 rounded-full bg-brand-indigo/20 blur-3xl" />
          <div className="pointer-events-none absolute -bottom-24 left-1/3 h-56 w-56 rounded-full bg-brand-cyan/15 blur-3xl" />
          <div className="relative">
            <div className="inline-flex items-center gap-2 rounded-full bg-white/5 px-3 py-1 text-[11px] font-medium uppercase tracking-wider text-cyan-300 ring-1 ring-white/10">
              <Icon.spark className="h-3.5 w-3.5" />
              Live operations
            </div>
            <h1 className="mt-4 font-display text-3xl font-semibold tracking-tight text-zinc-50 sm:text-4xl">
              Welcome back to <GradientText>Atlas</GradientText>
            </h1>
            <p className="mt-2 max-w-xl text-sm leading-relaxed text-zinc-400">
              Everything your assistant has done — and what it's doing right
              now, across every app on your phone.
            </p>
          </div>
        </div>
      </Reveal>

      {/* metrics */}
      <Stagger className="grid grid-cols-2 gap-4 sm:grid-cols-4">
        <StaggerItem>
          <Tile
            label="Tasks run"
            icon={<Icon.bolt className="h-4 w-4" />}
            accent="cyan"
            value={
              s.data ? <CountUp value={s.data.total_tasks} /> : "—"
            }
          />
        </StaggerItem>
        <StaggerItem>
          <Tile
            label="Success rate"
            icon={<Icon.check className="h-4 w-4" />}
            accent="emerald"
            value={
              s.data ? (
                <CountUp
                  value={Math.round(s.data.success_rate * 100)}
                  format={(n) => `${Math.round(n)}%`}
                />
              ) : (
                "—"
              )
            }
          />
        </StaggerItem>
        <StaggerItem>
          <Tile
            label="Top app"
            icon={<Icon.apps className="h-4 w-4" />}
            accent="violet"
            value={
              s.data?.top_app ? (
                <span className="flex items-center gap-2">
                  <span className="text-2xl">{s.data.top_app.emoji}</span>
                  <span className="truncate text-2xl">
                    {s.data.top_app.app_name}
                  </span>
                </span>
              ) : (
                "—"
              )
            }
          />
        </StaggerItem>
        <StaggerItem>
          <Link to="/live" className="block">
            <Tile
              label="Live run"
              icon={<Icon.arrow className="h-4 w-4" />}
              accent="indigo"
              value={<span className="text-cyan-300">Open</span>}
              sub="Watch the agent act in real time"
            />
          </Link>
        </StaggerItem>
      </Stagger>

      {/* recent activity */}
      <Reveal delay={0.1}>
        <Card className="p-5">
          <div className="mb-4 flex items-center justify-between">
            <div className="text-sm font-medium text-zinc-300">
              Recent activity
            </div>
            <Link
              to="/history"
              className="group inline-flex items-center gap-1 text-xs font-medium text-cyan-300 hover:text-cyan-200"
            >
              All history
              <Icon.arrow className="h-3.5 w-3.5 transition-transform group-hover:translate-x-0.5" />
            </Link>
          </div>
          {recent.loading ? (
            <SkeletonRows rows={6} />
          ) : recent.data && recent.data.items.length ? (
            <Stagger className="divide-y divide-white/5" gap={0.04}>
              {recent.data.items.map((t) => (
                <StaggerItem key={t.id}>
                  <Link
                    to={`/tasks/${t.id}`}
                    className="-mx-2 flex items-center gap-3 rounded-lg px-2 py-2.5 transition hover:bg-white/[0.04]"
                  >
                    <span className="grid h-9 w-9 shrink-0 place-items-center rounded-xl bg-white/5 text-lg ring-1 ring-white/5">
                      {t.emoji}
                    </span>
                    <span className="min-w-0 flex-1 truncate text-sm text-zinc-200">
                      {t.description}
                    </span>
                    <span className="hidden font-mono text-xs text-zinc-500 sm:block">
                      {fmtDur(t.duration_seconds)} · {fmtTime(t.started_at)}
                    </span>
                    <StatePill state={t.state} />
                  </Link>
                </StaggerItem>
              ))}
            </Stagger>
          ) : (
            <div className="text-sm text-zinc-500">
              Nothing yet — run a task from Telegram or voice and it shows up
              here.
            </div>
          )}
        </Card>
      </Reveal>
    </div>
  );
}
