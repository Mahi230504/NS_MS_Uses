import { ComparisonRow, useApi } from "../api";
import { Card, Empty, Skeleton, fmtTime } from "../components/bits";
import { Stagger, StaggerItem } from "../components/motion";
import { Icon } from "../components/icons";

export default function Comparisons() {
  const { data, loading } = useApi<{ items: ComparisonRow[] }>("/comparisons");
  return (
    <div className="space-y-5">
      <p className="text-sm text-zinc-500">
        The assistant checks the same thing across apps and picks the winner —
        cheapest, fastest, whatever you asked for.
      </p>
      {loading ? (
        <div className="space-y-3">
          {Array.from({ length: 3 }).map((_, i) => (
            <Skeleton key={i} className="h-32 rounded-2xl" />
          ))}
        </div>
      ) : data && data.items.length ? (
        <Stagger className="space-y-3" gap={0.05}>
          {data.items.map((c) => (
            <StaggerItem key={c.id}>
              <Card className="p-4">
                <div className="flex items-center justify-between gap-3">
                  <div className="min-w-0 font-medium text-zinc-100">
                    <span className="rounded-md bg-white/5 px-1.5 py-0.5 text-xs font-semibold uppercase tracking-wide text-cyan-300/90 ring-1 ring-white/5">
                      {c.ranking_key}
                    </span>
                    <span className="ml-2 text-zinc-300">“{c.query}”</span>
                  </div>
                  <div className="shrink-0 font-mono text-xs text-zinc-500">
                    {fmtTime(c.created_at)}
                  </div>
                </div>
                <div className="mt-3 grid gap-1.5">
                  {c.quotes.map((q, i) => {
                    const appId = String(q.app_id ?? "");
                    const isWinner = appId && appId === c.winner_app_id;
                    const isChosen = appId && appId === c.chosen_app_id;
                    const price = q.price as number | null;
                    return (
                      <div
                        key={i}
                        className={
                          "flex items-center justify-between rounded-xl px-3 py-2 text-sm ring-1 transition " +
                          (isWinner
                            ? "bg-emerald-500/10 ring-emerald-400/25"
                            : "bg-white/[0.03] ring-white/5")
                        }
                      >
                        <span className="flex items-center gap-2 text-zinc-200">
                          {isWinner && (
                            <Icon.trophy className="h-4 w-4 text-amber-300" />
                          )}
                          {String(q.app_name ?? q.app_id ?? "—")}
                          {isChosen && (
                            <span className="rounded-full bg-cyan-500/15 px-2 py-0.5 text-[10px] font-medium text-cyan-300 ring-1 ring-cyan-400/30">
                              ordered
                            </span>
                          )}
                        </span>
                        <span className="font-mono text-zinc-300">
                          {price != null ? `₹${price}` : String(q.notes ?? "—")}
                          {q.eta ? ` · ${q.eta}` : ""}
                        </span>
                      </div>
                    );
                  })}
                </div>
              </Card>
            </StaggerItem>
          ))}
        </Stagger>
      ) : (
        <Empty icon={<Icon.compare className="h-6 w-6" />}>
          No comparisons yet. Try “cheapest pizza on swiggy or zomato”.
        </Empty>
      )}
    </div>
  );
}
