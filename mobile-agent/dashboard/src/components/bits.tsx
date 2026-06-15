import { ReactNode } from "react";
import { motion } from "framer-motion";

export function Card({
  children,
  className = "",
  interactive = false,
  glow,
}: {
  children: ReactNode;
  className?: string;
  interactive?: boolean;
  glow?: "cyan" | "indigo" | "amber";
}) {
  const glowCls =
    glow === "cyan"
      ? "shadow-glow-cyan ring-brand-cyan/30"
      : glow === "indigo"
      ? "shadow-glow-indigo ring-brand-indigo/30"
      : glow === "amber"
      ? "shadow-glow-amber ring-amber-400/30"
      : "shadow-card ring-white/10";
  return (
    <motion.div
      whileHover={interactive ? { y: -3 } : undefined}
      transition={{ type: "spring", stiffness: 320, damping: 26 }}
      className={
        "group relative rounded-2xl bg-panel/70 ring-1 backdrop-blur-xl " +
        glowCls +
        " " +
        (interactive ? "cursor-pointer hover:ring-white/20 " : "") +
        className
      }
    >
      {children}
    </motion.div>
  );
}

const STATE_STYLE: Record<string, string> = {
  running: "bg-brand-cyan/15 text-cyan-200 ring-brand-cyan/40",
  done: "bg-emerald-500/15 text-emerald-300 ring-emerald-400/30",
  failed: "bg-rose-500/15 text-rose-300 ring-rose-400/30",
  timed_out: "bg-amber-500/15 text-amber-300 ring-amber-400/30",
  awaiting_approval: "bg-amber-500/15 text-amber-300 ring-amber-400/30",
};
const DOT: Record<string, string> = {
  running: "bg-brand-cyan",
  done: "bg-emerald-400",
  failed: "bg-rose-400",
  timed_out: "bg-amber-400",
  awaiting_approval: "bg-amber-400",
};

export function StatePill({ state }: { state: string }) {
  const cls = STATE_STYLE[state] || "bg-zinc-500/15 text-zinc-300 ring-zinc-400/30";
  const dot = DOT[state] || "bg-zinc-400";
  return (
    <span
      className={`inline-flex items-center gap-1.5 whitespace-nowrap rounded-full px-2.5 py-0.5 text-xs font-medium ring-1 ${cls}`}
    >
      {state === "running" ? (
        <span className="relative flex h-2 w-2">
          <span className="absolute inline-flex h-full w-full rounded-full bg-brand-cyan animate-ping2" />
          <span className="relative inline-flex h-2 w-2 rounded-full bg-brand-cyan" />
        </span>
      ) : (
        <span className={`h-1.5 w-1.5 rounded-full ${dot}`} />
      )}
      {state.replace(/_/g, " ")}
    </span>
  );
}

export function Tile({
  label,
  value,
  sub,
  icon,
  accent = "cyan",
}: {
  label: string;
  value: ReactNode;
  sub?: ReactNode;
  icon?: ReactNode;
  accent?: "cyan" | "indigo" | "emerald" | "violet";
}) {
  const bar =
    accent === "indigo"
      ? "from-brand-indigo/0 via-brand-indigo to-brand-indigo/0"
      : accent === "emerald"
      ? "from-emerald-400/0 via-emerald-400 to-emerald-400/0"
      : accent === "violet"
      ? "from-brand-violet/0 via-brand-violet to-brand-violet/0"
      : "from-brand-cyan/0 via-brand-cyan to-brand-cyan/0";
  return (
    <Card interactive className="overflow-hidden p-4">
      <span
        className={`absolute inset-x-5 top-0 h-px bg-gradient-to-r ${bar} opacity-60`}
      />
      <div className="flex items-start justify-between">
        <div className="text-[11px] font-medium uppercase tracking-wider text-zinc-500">
          {label}
        </div>
        {icon && <div className="text-zinc-500 transition-colors group-hover:text-zinc-300">{icon}</div>}
      </div>
      <div className="mt-2 font-display text-3xl font-semibold tracking-tight text-zinc-50">
        {value}
      </div>
      {sub && <div className="mt-1 text-xs text-zinc-500">{sub}</div>}
    </Card>
  );
}

export function Empty({
  children,
  icon,
}: {
  children: ReactNode;
  icon?: ReactNode;
}) {
  return (
    <div className="grid place-items-center rounded-2xl border border-dashed border-white/10 bg-white/[0.015] p-12 text-center">
      {icon && (
        <div className="mb-3 grid h-12 w-12 place-items-center rounded-2xl bg-brand-soft text-cyan-200 ring-1 ring-white/10">
          {icon}
        </div>
      )}
      <div className="max-w-md text-sm text-zinc-400">{children}</div>
    </div>
  );
}

export function Spinner({ label }: { label?: string }) {
  return (
    <div className="flex items-center gap-3 text-zinc-400">
      <span className="h-4 w-4 animate-spin rounded-full border-2 border-zinc-700 border-t-brand-cyan" />
      {label || "Loading…"}
    </div>
  );
}

/** Shimmering placeholder block. Compose for skeleton screens. */
export function Skeleton({ className = "" }: { className?: string }) {
  return <div className={"skeleton " + className} />;
}

/** A stack of skeleton rows for list-style loading. */
export function SkeletonRows({ rows = 5 }: { rows?: number }) {
  return (
    <div className="space-y-2.5">
      {Array.from({ length: rows }).map((_, i) => (
        <div key={i} className="flex items-center gap-3">
          <Skeleton className="h-9 w-9 rounded-xl" />
          <Skeleton className="h-3.5 flex-1" />
          <Skeleton className="h-3.5 w-16" />
        </div>
      ))}
    </div>
  );
}

export function GradientText({
  children,
  className = "",
}: {
  children: ReactNode;
  className?: string;
}) {
  return <span className={"text-gradient " + className}>{children}</span>;
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
