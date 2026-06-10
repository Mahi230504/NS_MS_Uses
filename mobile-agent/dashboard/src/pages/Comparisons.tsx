import { ComparisonRow, useApi } from "../api";
import { Card, Empty, Spinner, fmtTime } from "../components/bits";

export default function Comparisons() {
  const { data, loading } = useApi<{ items: ComparisonRow[] }>("/comparisons");
  return (
    <div className="space-y-5">
      <h1 className="text-xl font-semibold text-zinc-100">Cross-app comparisons</h1>
      {loading ? (
        <Spinner />
      ) : data && data.items.length ? (
        <div className="space-y-3">
          {data.items.map((c) => (
            <Card key={c.id} className="p-4">
              <div className="flex items-center justify-between">
                <div className="font-medium text-zinc-100">
                  {c.ranking_key} · “{c.query}”
                </div>
                <div className="text-xs text-zinc-500">{fmtTime(c.created_at)}</div>
              </div>
              <div className="mt-2 grid gap-1">
                {c.quotes.map((q, i) => {
                  const appId = String(q.app_id ?? "");
                  const isWinner = appId && appId === c.winner_app_id;
                  const isChosen = appId && appId === c.chosen_app_id;
                  const price = q.price as number | null;
                  return (
                    <div
                      key={i}
                      className={
                        "flex items-center justify-between rounded-lg px-3 py-1.5 text-sm " +
                        (isWinner ? "bg-emerald-500/10" : "bg-white/5")
                      }
                    >
                      <span className="text-zinc-200">
                        {String(q.app_name ?? q.app_id ?? "—")}
                        {isWinner && " 🏆"}
                        {isChosen && " · ordered"}
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
          ))}
        </div>
      ) : (
        <Empty>
          No comparisons yet. Try “cheapest pizza on swiggy or zomato”.
        </Empty>
      )}
    </div>
  );
}
