import { useState } from "react";
import { NavLink, Route, Routes, useNavigate } from "react-router-dom";
import { getToken, setToken } from "./api";
import { useEventStream, useLive } from "./live";
import Overview from "./pages/Overview";
import LiveRun from "./pages/LiveRun";
import History from "./pages/History";
import TaskDetail from "./pages/TaskDetail";
import Saved from "./pages/Saved";
import Schedules from "./pages/Schedules";
import Comparisons from "./pages/Comparisons";
import Analytics from "./pages/Analytics";

const NAV = [
  { to: "/", label: "Overview", icon: "◎", end: true },
  { to: "/live", label: "Live run", icon: "⦿" },
  { to: "/history", label: "History", icon: "≡" },
  { to: "/comparisons", label: "Comparisons", icon: "⇄" },
  { to: "/saved", label: "Saved", icon: "★" },
  { to: "/schedules", label: "Schedules", icon: "⏱" },
  { to: "/analytics", label: "Apps", icon: "▦" },
];

function TokenGate({ onSet }: { onSet: () => void }) {
  const [v, setV] = useState("");
  return (
    <div className="grid min-h-screen place-items-center p-6">
      <div className="w-full max-w-sm rounded-2xl bg-panel/70 p-6 ring-1 ring-white/10">
        <div className="text-lg font-semibold text-zinc-100">Atlas</div>
        <p className="mt-1 text-sm text-zinc-500">
          Enter your dashboard token (the <code>DASHBOARD_TOKEN</code> from the
          bot's config) to connect.
        </p>
        <input
          autoFocus
          value={v}
          onChange={(e) => setV(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && v && (setToken(v), onSet())}
          placeholder="dashboard token"
          className="mt-4 w-full rounded-lg bg-black/40 px-3 py-2 text-sm text-zinc-100 ring-1 ring-white/10 outline-none focus:ring-cyan-400/40"
        />
        <button
          onClick={() => v && (setToken(v), onSet())}
          className="mt-3 w-full rounded-lg bg-cyan-500/90 px-3 py-2 text-sm font-medium text-black hover:bg-cyan-400"
        >
          Connect
        </button>
      </div>
    </div>
  );
}

function LiveBadge() {
  const navigate = useNavigate();
  const connected = useLive((s) => s.connected);
  const state = useLive((s) => s.state);
  const running = state === "running";
  return (
    <button
      onClick={() => navigate("/live")}
      className="flex items-center gap-2 rounded-full bg-white/5 px-3 py-1 text-xs text-zinc-300 ring-1 ring-white/10 hover:bg-white/10"
      title={connected ? "stream connected" : "stream disconnected"}
    >
      <span
        className={`h-2 w-2 rounded-full ${
          running ? "bg-cyan-400 animate-ping2" : connected ? "bg-emerald-400" : "bg-zinc-600"
        }`}
      />
      {running ? "task running" : connected ? "connected" : "offline"}
    </button>
  );
}

export default function App() {
  const [authed, setAuthed] = useState(!!getToken());
  if (!authed) return <TokenGate onSet={() => setAuthed(true)} />;
  return <Shell />;
}

function Shell() {
  useEventStream();
  return (
    <div className="flex min-h-screen">
      <aside className="sticky top-0 hidden h-screen w-56 shrink-0 flex-col border-r border-white/5 bg-panel/40 p-4 sm:flex">
        <div className="px-2 text-lg font-semibold tracking-tight text-zinc-100">
          Atlas
          <span className="ml-1 text-cyan-400">·</span>
        </div>
        <div className="mt-1 px-2 text-[11px] text-zinc-500">
          your assistant, watching everything
        </div>
        <nav className="mt-6 flex flex-col gap-1">
          {NAV.map((n) => (
            <NavLink
              key={n.to}
              to={n.to}
              end={n.end}
              className={({ isActive }) =>
                "flex items-center gap-3 rounded-lg px-3 py-2 text-sm " +
                (isActive
                  ? "bg-cyan-500/10 text-cyan-300 ring-1 ring-cyan-400/20"
                  : "text-zinc-400 hover:bg-white/5 hover:text-zinc-200")
              }
            >
              <span className="w-4 text-center text-zinc-500">{n.icon}</span>
              {n.label}
            </NavLink>
          ))}
        </nav>
      </aside>

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="sticky top-0 z-10 flex items-center justify-between border-b border-white/5 bg-ink/80 px-6 py-3 backdrop-blur">
          <div className="text-sm text-zinc-500">Personal assistant</div>
          <LiveBadge />
        </header>
        <main className="flex-1 p-6">
          <Routes>
            <Route path="/" element={<Overview />} />
            <Route path="/live" element={<LiveRun />} />
            <Route path="/history" element={<History />} />
            <Route path="/tasks/:id" element={<TaskDetail />} />
            <Route path="/saved" element={<Saved />} />
            <Route path="/schedules" element={<Schedules />} />
            <Route path="/comparisons" element={<Comparisons />} />
            <Route path="/analytics" element={<Analytics />} />
          </Routes>
        </main>
      </div>
    </div>
  );
}
