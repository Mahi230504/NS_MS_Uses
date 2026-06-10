import { SavedRow, useApi } from "../api";
import { Card, Empty, Spinner, fmtTime } from "../components/bits";

export default function Saved() {
  const { data, loading } = useApi<{ items: SavedRow[] }>("/saved");
  return (
    <div className="space-y-5">
      <h1 className="text-xl font-semibold text-zinc-100">Saved quick tasks</h1>
      {loading ? (
        <Spinner />
      ) : data && data.items.length ? (
        <div className="grid gap-3 sm:grid-cols-2">
          {data.items.map((s) => (
            <Card key={s.id} className="p-4">
              <div className="flex items-center gap-2">
                <span className="text-lg">{s.emoji}</span>
                <div className="font-medium text-zinc-100">{s.label}</div>
              </div>
              <div className="mt-1 text-sm text-zinc-400">{s.description}</div>
              <div className="mt-2 flex items-center gap-3 text-xs text-zinc-500">
                <code className="rounded bg-white/5 px-1.5 py-0.5">/run {s.slug}</code>
                <span>· run {s.run_count}×</span>
                {s.last_run_at && <span>· last {fmtTime(s.last_run_at)}</span>}
              </div>
            </Card>
          ))}
        </div>
      ) : (
        <Empty>
          No saved tasks yet. After a run, tap “Save as quick task” in Telegram
          (or say “save this as …”).
        </Empty>
      )}
    </div>
  );
}
