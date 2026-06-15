import { SavedRow, useApi } from "../api";
import { Card, Empty, Skeleton, fmtTime } from "../components/bits";
import { Stagger, StaggerItem } from "../components/motion";
import { Icon } from "../components/icons";

export default function Saved() {
  const { data, loading } = useApi<{ items: SavedRow[] }>("/saved");
  return (
    <div className="space-y-5">
      <p className="text-sm text-zinc-500">
        One-tap tasks you've taught the assistant — run any of them by voice or
        with <code className="rounded bg-white/5 px-1 py-0.5 font-mono text-[11px]">/run</code>.
      </p>
      {loading ? (
        <div className="grid gap-3 sm:grid-cols-2">
          {Array.from({ length: 4 }).map((_, i) => (
            <Skeleton key={i} className="h-28 rounded-2xl" />
          ))}
        </div>
      ) : data && data.items.length ? (
        <Stagger className="grid gap-3 sm:grid-cols-2" gap={0.05}>
          {data.items.map((s) => (
            <StaggerItem key={s.id}>
              <Card interactive className="h-full p-4">
                <div className="flex items-center gap-2.5">
                  <span className="grid h-9 w-9 place-items-center rounded-xl bg-white/5 text-lg ring-1 ring-white/5">
                    {s.emoji}
                  </span>
                  <div className="font-medium text-zinc-100">{s.label}</div>
                </div>
                <div className="mt-2 text-sm text-zinc-400">{s.description}</div>
                <div className="mt-3 flex flex-wrap items-center gap-2 text-xs text-zinc-500">
                  <code className="rounded-md bg-white/5 px-1.5 py-0.5 font-mono text-cyan-300/90 ring-1 ring-white/5">
                    /run {s.slug}
                  </code>
                  <span className="font-mono">· run {s.run_count}×</span>
                  {s.last_run_at && (
                    <span className="font-mono">· last {fmtTime(s.last_run_at)}</span>
                  )}
                </div>
              </Card>
            </StaggerItem>
          ))}
        </Stagger>
      ) : (
        <Empty icon={<Icon.saved className="h-6 w-6" />}>
          No saved tasks yet. After a run, tap “Save as quick task” in Telegram
          (or say “save this as …”).
        </Empty>
      )}
    </div>
  );
}
