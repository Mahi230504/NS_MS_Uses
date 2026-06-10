import { ReactNode } from "react";

export function Card({
  children,
  className = "",
}: {
  children: ReactNode;
  className?: string;
}) {
  return (
    <div
      className={
        "rounded-2xl bg-panel/70 ring-1 ring-white/5 backdrop-blur " +
        "shadow-lg shadow-black/30 " +
        className
      }
    >
      {children}
    </div>
  );
}

const STATE_STYLE: Record<string, string> = {
  running: "bg-cyan-500/15 text-cyan-300 ring-cyan-400/30",
  done: "bg-emerald-500/15 text-emerald-300 ring-emerald-400/30",
  failed: "bg-red-500/15 text-red-300 ring-red-400/30",
  timed_out: "bg-amber-500/15 text-amber-300 ring-amber-400/30",
  awaiting_approval: "bg-amber-500/15 text-amber-300 ring-amber-400/30",
};

export function StatePill({ state }: { state: string }) {
  const cls = STATE_STYLE[state] || "bg-zinc-500/15 text-zinc-300 ring-zinc-400/30";
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-xs font-medium ring-1 ${cls}`}
    >
      {state === "running" && (
        <span className="relative flex h-2 w-2">
          <span className="absolute inline-flex h-full w-full rounded-full bg-cyan-400 animate-ping2" />
          <span className="relative inline-flex h-2 w-2 rounded-full bg-cyan-400" />
        </span>
      )}
      {state.replace(/_/g, " ")}
    </span>
  );
}

export function Tile({
  label,
  value,
  sub,
}: {
  label: string;
  value: ReactNode;
  sub?: string;
}) {
  return (
    <Card className="p-4">
      <div className="text-xs uppercase tracking-wide text-zinc-500">{label}</div>
      <div className="mt-1 text-2xl font-semibold text-zinc-100">{value}</div>
      {sub && <div className="mt-0.5 text-xs text-zinc-500">{sub}</div>}
    </Card>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return (
    <div className="rounded-2xl border border-dashed border-white/10 p-10 text-center text-zinc-500">
      {children}
    </div>
  );
}

export function Spinner({ label }: { label?: string }) {
  return (
    <div className="flex items-center gap-3 text-zinc-400">
      <span className="h-4 w-4 animate-spin rounded-full border-2 border-zinc-600 border-t-cyan-400" />
      {label || "Loading…"}
    </div>
  );
}

export const fmtTime = (iso: string | null) => {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleString(undefined, {
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return iso;
  }
};

export const fmtDur = (s: number | null) =>
  s == null ? "—" : s < 60 ? `${s.toFixed(0)}s` : `${Math.floor(s / 60)}m ${Math.round(s % 60)}s`;
