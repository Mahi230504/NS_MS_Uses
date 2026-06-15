import { useState } from "react";
import {
  NavLink,
  Route,
  Routes,
  useLocation,
  useNavigate,
} from "react-router-dom";
import { AnimatePresence, motion } from "framer-motion";
import { getToken, setToken } from "./api";
import { useEventStream, useLive } from "./live";
import { Icon, Logo } from "./components/icons";
import { PageTransition } from "./components/motion";
import Overview from "./pages/Overview";
import LiveRun from "./pages/LiveRun";
import History from "./pages/History";
import TaskDetail from "./pages/TaskDetail";
import Saved from "./pages/Saved";
import Schedules from "./pages/Schedules";
import Comparisons from "./pages/Comparisons";
import Analytics from "./pages/Analytics";
import Settings from "./pages/Settings";

type NavItem = {
  to: string;
  label: string;
  icon: keyof typeof Icon;
  end?: boolean;
};

const NAV: NavItem[] = [
  { to: "/", label: "Overview", icon: "overview", end: true },
  { to: "/live", label: "Live run", icon: "live" },
  { to: "/history", label: "History", icon: "history" },
  { to: "/comparisons", label: "Comparisons", icon: "compare" },
  { to: "/saved", label: "Saved", icon: "saved" },
  { to: "/schedules", label: "Schedules", icon: "schedules" },
  { to: "/analytics", label: "Apps", icon: "apps" },
  { to: "/settings", label: "Settings", icon: "settings" },
];

const TITLES: Record<string, string> = {
  "/": "Overview",
  "/live": "Live run",
  "/history": "History",
  "/comparisons": "Comparisons",
  "/saved": "Saved tasks",
  "/schedules": "Schedules",
  "/analytics": "Apps",
  "/settings": "Settings",
};

function TokenGate({ onSet }: { onSet: () => void }) {
  const [v, setV] = useState("");
  const submit = () => v && (setToken(v), onSet());
  return (
    <div className="grid min-h-dvh place-items-center p-6">
      <motion.div
        initial={{ opacity: 0, y: 18, scale: 0.98 }}
        animate={{ opacity: 1, y: 0, scale: 1 }}
        transition={{ duration: 0.6, ease: [0.22, 1, 0.36, 1] }}
        className="glass w-full max-w-sm p-7"
      >
        <div className="flex items-center gap-3">
          <Logo className="h-12 w-12 animate-float" />
          <div>
            <div className="font-display text-xl font-semibold tracking-tight text-zinc-50">
              Atlas
            </div>
            <div className="text-[11px] uppercase tracking-[0.2em] text-cyan-300/80">
              Mission Control
            </div>
          </div>
        </div>
        <p className="mt-5 text-sm leading-relaxed text-zinc-400">
          Enter your dashboard token — the{" "}
          <code className="rounded bg-white/5 px-1.5 py-0.5 font-mono text-[12px] text-zinc-300">
            DASHBOARD_TOKEN
          </code>{" "}
          from the bot's config — to connect.
        </p>
        <input
          autoFocus
          value={v}
          onChange={(e) => setV(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && submit()}
          placeholder="dashboard token"
          className="mt-5 w-full rounded-xl bg-black/40 px-3.5 py-2.5 text-sm text-zinc-100 ring-1 ring-white/10 outline-none transition focus:ring-2 focus:ring-brand-cyan/50"
        />
        <button
          onClick={submit}
          className="group mt-3 flex w-full items-center justify-center gap-2 rounded-xl bg-brand bg-[length:200%_200%] px-3 py-2.5 text-sm font-semibold text-ink transition hover:animate-gradient-x hover:shadow-glow-cyan"
        >
          Connect
          <Icon.arrow className="h-4 w-4 transition-transform group-hover:translate-x-0.5" />
        </button>
      </motion.div>
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
      className="flex items-center gap-2 rounded-full bg-white/5 px-3 py-1.5 text-xs font-medium text-zinc-300 ring-1 ring-white/10 transition hover:bg-white/10"
      title={connected ? "stream connected" : "stream disconnected"}
    >
      <span className="relative flex h-2 w-2">
        {running && (
          <span className="absolute inline-flex h-full w-full rounded-full bg-brand-cyan animate-ping2" />
        )}
        <span
          className={`relative inline-flex h-2 w-2 rounded-full ${
            running ? "bg-brand-cyan" : connected ? "bg-emerald-400" : "bg-zinc-600"
          }`}
        />
      </span>
      {running ? "task running" : connected ? "connected" : "offline"}
    </button>
  );
}

function NavRow({ n, onNavigate }: { n: NavItem; onNavigate?: () => void }) {
  const I = Icon[n.icon];
  return (
    <NavLink
      to={n.to}
      end={n.end}
      onClick={onNavigate}
      className={({ isActive }) =>
        "relative flex items-center gap-3 rounded-xl px-3 py-2.5 text-sm transition-colors " +
        (isActive
          ? "text-cyan-200"
          : "text-zinc-400 hover:bg-white/5 hover:text-zinc-100")
      }
    >
      {({ isActive }) => (
        <>
          {isActive && (
            <motion.span
              layoutId="nav-active"
              transition={{ type: "spring", stiffness: 420, damping: 34 }}
              className="absolute inset-0 -z-10 rounded-xl bg-brand-soft ring-1 ring-brand-cyan/25"
            />
          )}
          <I className="h-[18px] w-[18px] shrink-0" />
          <span className="font-medium">{n.label}</span>
        </>
      )}
    </NavLink>
  );
}

function Brand() {
  return (
    <div className="flex items-center gap-3 px-1">
      <Logo />
      <div>
        <div className="font-display text-lg font-semibold leading-none tracking-tight text-zinc-50">
          Atlas
        </div>
        <div className="mt-1 text-[10px] uppercase tracking-[0.18em] text-cyan-300/70">
          Mission Control
        </div>
      </div>
    </div>
  );
}

export default function App() {
  const [authed, setAuthed] = useState(!!getToken());
  if (!authed) return <TokenGate onSet={() => setAuthed(true)} />;
  return <Shell />;
}

function Shell() {
  useEventStream();
  const location = useLocation();
  const [drawer, setDrawer] = useState(false);
  const title = TITLES[location.pathname] ?? "Atlas";

  return (
    <div className="flex min-h-dvh">
      {/* desktop sidebar */}
      <aside className="sticky top-0 hidden h-dvh w-60 shrink-0 flex-col border-r border-white/5 bg-panel/30 p-4 backdrop-blur-xl sm:flex">
        <Brand />
        <p className="mt-3 px-1 text-[11px] leading-relaxed text-zinc-500">
          Your assistant, watching everything.
        </p>
        <nav className="mt-6 flex flex-col gap-1">
          {NAV.map((n) => (
            <NavRow key={n.to} n={n} />
          ))}
        </nav>
        <div className="mt-auto rounded-xl bg-white/[0.03] p-3 ring-1 ring-white/5">
          <div className="flex items-center gap-2 text-[11px] text-zinc-500">
            <Icon.device className="h-4 w-4" />
            Connected device
          </div>
          <div className="mt-1.5">
            <LiveBadge />
          </div>
        </div>
      </aside>

      {/* mobile slide-over drawer */}
      <AnimatePresence>
        {drawer && (
          <>
            <motion.div
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              exit={{ opacity: 0 }}
              onClick={() => setDrawer(false)}
              className="fixed inset-0 z-40 bg-black/60 backdrop-blur-sm sm:hidden"
            />
            <motion.aside
              initial={{ x: "-100%" }}
              animate={{ x: 0 }}
              exit={{ x: "-100%" }}
              transition={{ type: "spring", stiffness: 360, damping: 36 }}
              className="fixed inset-y-0 left-0 z-50 flex w-72 flex-col border-r border-white/10 bg-panel/95 p-4 backdrop-blur-xl sm:hidden"
            >
              <div className="flex items-center justify-between">
                <Brand />
                <button
                  onClick={() => setDrawer(false)}
                  aria-label="Close menu"
                  className="grid h-9 w-9 place-items-center rounded-lg text-zinc-400 hover:bg-white/5 hover:text-zinc-100"
                >
                  <Icon.x className="h-5 w-5" />
                </button>
              </div>
              <nav className="mt-6 flex flex-col gap-1">
                {NAV.map((n) => (
                  <NavRow key={n.to} n={n} onNavigate={() => setDrawer(false)} />
                ))}
              </nav>
            </motion.aside>
          </>
        )}
      </AnimatePresence>

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="sticky top-0 z-30 flex items-center justify-between gap-3 border-b border-white/5 bg-ink/70 px-4 py-3 backdrop-blur-xl sm:px-6">
          <div className="flex items-center gap-3">
            <button
              onClick={() => setDrawer(true)}
              aria-label="Open menu"
              className="grid h-9 w-9 place-items-center rounded-lg text-zinc-400 ring-1 ring-white/10 hover:bg-white/5 hover:text-zinc-100 sm:hidden"
            >
              <Icon.menu className="h-5 w-5" />
            </button>
            <h1 className="font-display text-base font-semibold tracking-tight text-zinc-100">
              {title}
            </h1>
          </div>
          <div className="sm:hidden">
            <LiveBadge />
          </div>
          <div className="hidden text-xs text-zinc-500 sm:block">
            Personal assistant
          </div>
        </header>

        <main className="mx-auto w-full max-w-6xl flex-1 p-4 sm:p-6">
          <AnimatePresence mode="wait">
            <PageTransition key={location.pathname}>
              <Routes location={location}>
                <Route path="/" element={<Overview />} />
                <Route path="/live" element={<LiveRun />} />
                <Route path="/history" element={<History />} />
                <Route path="/tasks/:id" element={<TaskDetail />} />
                <Route path="/saved" element={<Saved />} />
                <Route path="/schedules" element={<Schedules />} />
                <Route path="/comparisons" element={<Comparisons />} />
                <Route path="/analytics" element={<Analytics />} />
                <Route path="/settings" element={<Settings />} />
              </Routes>
            </PageTransition>
          </AnimatePresence>
        </main>
      </div>
    </div>
  );
}
