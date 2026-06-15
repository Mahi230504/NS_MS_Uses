import { useEffect, useState } from "react";
import {
  CloudActionRow,
  ContactRow,
  GoogleStatus,
  apiDelete,
  apiPost,
  useApi,
} from "../api";
import { Card, Skeleton, fmtTime } from "../components/bits";
import { Stagger, StaggerItem } from "../components/motion";
import { Icon } from "../components/icons";

const inputCls =
  "rounded-xl bg-black/40 px-3.5 py-2.5 text-sm text-zinc-100 ring-1 ring-white/10 outline-none transition focus:ring-2 focus:ring-brand-cyan/50 placeholder:text-zinc-600";
const primaryBtn =
  "inline-flex items-center justify-center gap-1.5 rounded-xl bg-brand bg-[length:200%_200%] px-3.5 py-2.5 text-sm font-semibold text-ink transition hover:animate-gradient-x hover:shadow-glow-cyan disabled:opacity-50 disabled:hover:shadow-none";
const dangerBtn =
  "inline-flex items-center justify-center gap-1.5 rounded-xl bg-rose-500/20 px-3.5 py-2.5 text-sm font-semibold text-rose-200 ring-1 ring-rose-400/40 transition hover:bg-rose-500/30 disabled:opacity-50";

function CardHeader({
  icon,
  title,
  desc,
}: {
  icon: React.ReactNode;
  title: string;
  desc: React.ReactNode;
}) {
  return (
    <div className="flex items-start gap-3">
      <span className="grid h-9 w-9 shrink-0 place-items-center rounded-xl bg-brand-soft text-cyan-200 ring-1 ring-white/10">
        {icon}
      </span>
      <div>
        <div className="text-sm font-semibold text-zinc-100">{title}</div>
        <p className="mt-0.5 text-xs leading-relaxed text-zinc-500">{desc}</p>
      </div>
    </div>
  );
}

function StatusPill({ status }: { status: string }) {
  const ok = status === "ok";
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-xs font-medium ring-1 ${
        ok
          ? "bg-emerald-500/15 text-emerald-300 ring-emerald-400/30"
          : "bg-rose-500/15 text-rose-300 ring-rose-400/30"
      }`}
    >
      <span
        className={`h-1.5 w-1.5 rounded-full ${ok ? "bg-emerald-400" : "bg-rose-400"}`}
      />
      {status}
    </span>
  );
}

function GoogleCard() {
  const [bump, setBump] = useState(0);
  const { data, loading } = useApi<GoogleStatus>("/google/status", [bump]);
  const [busy, setBusy] = useState(false);

  // Google redirects back to /settings?google=connected|error — show it once,
  // then scrub the param so refreshes don't re-announce it.
  const [banner, setBanner] = useState<"connected" | "error" | null>(null);
  useEffect(() => {
    const p = new URLSearchParams(window.location.search).get("google");
    if (p === "connected" || p === "error") {
      setBanner(p);
      window.history.replaceState({}, "", window.location.pathname);
    }
  }, []);

  const connect = async () => {
    setBusy(true);
    try {
      const r = await apiPost<{ auth_url: string }>("/google/connect", {});
      window.location.href = r.auth_url;
    } catch {
      setBusy(false);
    }
  };

  const disconnect = async () => {
    setBusy(true);
    try {
      await apiPost("/google/disconnect", {});
      setBump((b) => b + 1);
    } catch {
      /* leave the card as-is so the user can retry */
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card className="p-5">
      <CardHeader
        icon={<Icon.mail className="h-[18px] w-[18px]" />}
        title="Google account"
        desc="Powers cloud actions — Gmail sends and Google Meet invites, no phone needed."
      />
      {banner && (
        <div
          className={
            "mt-4 flex items-center gap-2 rounded-xl p-3 text-sm ring-1 " +
            (banner === "connected"
              ? "bg-emerald-500/10 text-emerald-200 ring-emerald-400/20"
              : "bg-rose-500/10 text-rose-200 ring-rose-400/20")
          }
        >
          {banner === "connected" ? (
            <Icon.check className="h-4 w-4" />
          ) : (
            <Icon.x className="h-4 w-4" />
          )}
          {banner === "connected"
            ? "Google connected — you're all set."
            : "Google connection failed — try again."}
        </div>
      )}
      {loading ? (
        <Skeleton className="mt-4 h-10 w-full" />
      ) : !data ? null : !data.configured ? (
        <div className="mt-4 text-sm text-zinc-400">
          Not configured. Set{" "}
          <code className="rounded bg-white/5 px-1.5 py-0.5 font-mono text-xs">
            GOOGLE_CLIENT_ID
          </code>{" "}
          and{" "}
          <code className="rounded bg-white/5 px-1.5 py-0.5 font-mono text-xs">
            GOOGLE_CLIENT_SECRET
          </code>{" "}
          in the bot's{" "}
          <code className="rounded bg-white/5 px-1.5 py-0.5 font-mono text-xs">
            .env
          </code>
          , then restart.
        </div>
      ) : data.connected ? (
        <div className="mt-4 space-y-3">
          <div className="flex items-center gap-2">
            <span className="h-2 w-2 rounded-full bg-emerald-400" />
            <span className="text-sm text-zinc-200">{data.email}</span>
            <span className="text-xs text-zinc-500">· default Gmail</span>
          </div>
          <div className="flex flex-wrap gap-1.5">
            {data.scopes.map((s) => (
              <span
                key={s}
                className="rounded-full bg-white/5 px-2 py-0.5 font-mono text-[10px] text-zinc-400 ring-1 ring-white/10"
              >
                {s.replace("https://www.googleapis.com/auth/", "")}
              </span>
            ))}
          </div>
          {data.connected_at && (
            <div className="font-mono text-xs text-zinc-500">
              connected {fmtTime(data.connected_at)}
            </div>
          )}
          <button disabled={busy} onClick={disconnect} className={dangerBtn}>
            Disconnect
          </button>
        </div>
      ) : (
        <button disabled={busy} onClick={connect} className={primaryBtn + " mt-4"}>
          Connect Google
          <Icon.arrow className="h-4 w-4" />
        </button>
      )}
    </Card>
  );
}

function ContactsCard() {
  const [bump, setBump] = useState(0);
  const { data, loading } = useApi<{ items: ContactRow[] }>("/contacts", [bump]);
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [busy, setBusy] = useState(false);

  const add = async () => {
    if (!name.trim() || !email.trim()) return;
    setBusy(true);
    try {
      await apiPost("/contacts", { name: name.trim(), email: email.trim() });
      setName("");
      setEmail("");
      setBump((b) => b + 1);
    } catch {
      /* invalid email etc. — keep the inputs so the user can fix them */
    } finally {
      setBusy(false);
    }
  };

  const remove = async (n: string) => {
    setBusy(true);
    try {
      await apiDelete(`/contacts/${encodeURIComponent(n)}`);
      setBump((b) => b + 1);
    } catch {
      /* row stays; retry is one click away */
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card className="p-5">
      <CardHeader
        icon={<Icon.saved className="h-[18px] w-[18px]" />}
        title="Contacts"
        desc={
          <>
            Names the assistant can resolve by voice — "meet with Ayush" works
            once Ayush is here.
          </>
        }
      />
      <div className="mt-4 flex gap-2">
        <input
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="name"
          className={inputCls + " w-28"}
        />
        <input
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && add()}
          placeholder="email@example.com"
          type="email"
          className={inputCls + " min-w-0 flex-1"}
        />
        <button
          disabled={busy || !name.trim() || !email.trim()}
          onClick={add}
          className={primaryBtn}
        >
          Add
        </button>
      </div>
      {loading ? (
        <Skeleton className="mt-4 h-10 w-full" />
      ) : data && data.items.length ? (
        <ul className="mt-3 divide-y divide-white/5">
          {data.items.map((c) => (
            <li key={c.id} className="flex items-center gap-3 py-2">
              <span className="text-sm text-zinc-200">{c.name}</span>
              <span className="min-w-0 flex-1 truncate font-mono text-xs text-zinc-500">
                {c.email}
              </span>
              <button
                disabled={busy}
                onClick={() => remove(c.name)}
                className="rounded-lg px-2 py-1 text-xs text-zinc-500 transition hover:bg-rose-500/10 hover:text-rose-300 disabled:opacity-50"
              >
                remove
              </button>
            </li>
          ))}
        </ul>
      ) : (
        <div className="mt-3 text-sm text-zinc-500">
          No contacts yet — add one above or use /contact in Telegram.
        </div>
      )}
    </Card>
  );
}

function CommandCard() {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [reply, setReply] = useState<string | null>(null);

  const run = async () => {
    if (!text.trim() || busy) return;
    setBusy(true);
    try {
      const r = await apiPost<{ ok: boolean; message: string }>("/command", {
        text: text.trim(),
      });
      setReply(r.message);
      setText("");
    } catch (e) {
      setReply(String((e as Error).message || e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card className="p-5">
      <CardHeader
        icon={<Icon.bolt className="h-[18px] w-[18px]" />}
        title="Command"
        desc={
          <>
            Type anything you'd say to the assistant — "email alice@x.com that
            the demo is ready", "meet with Ayush tomorrow 3pm".
          </>
        }
      />
      <div className="mt-4 flex gap-2">
        <input
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && run()}
          placeholder="what should Atlas do?"
          className={inputCls + " min-w-0 flex-1"}
        />
        <button disabled={busy || !text.trim()} onClick={run} className={primaryBtn}>
          {busy ? "Running…" : "Run"}
        </button>
      </div>
      {reply && (
        <div className="mt-3 rounded-xl bg-white/5 p-3 text-sm text-zinc-300 ring-1 ring-white/5">
          {reply}
        </div>
      )}
    </Card>
  );
}

function summarize(a: CloudActionRow): string {
  const p = a.payload;
  if (a.kind === "email") {
    const to = Array.isArray(p.to) ? (p.to as unknown[]).join(", ") : "";
    return `“${String(p.subject ?? "")}”${to ? ` → ${to}` : ""}`;
  }
  const who = Array.isArray(p.attendees) ? (p.attendees as unknown[]).join(", ") : "";
  return `${String(p.title ?? "")}${who ? ` → ${who}` : ""}`;
}

function CloudActionsCard() {
  const { data, loading } = useApi<{ items: CloudActionRow[] }>("/cloud-actions");
  return (
    <Card className="p-5">
      <CardHeader
        icon={<Icon.calendar className="h-[18px] w-[18px]" />}
        title="Recent cloud actions"
        desc="Emails sent and meetings scheduled straight from the cloud — no phone."
      />
      {loading ? (
        <Skeleton className="mt-4 h-10 w-full" />
      ) : data && data.items.length ? (
        <ul className="mt-3 divide-y divide-white/5">
          {data.items.map((a) => (
            <li key={a.id} className="flex items-center gap-3 py-2.5">
              <span className="grid h-8 w-8 shrink-0 place-items-center rounded-lg bg-white/5 text-zinc-300 ring-1 ring-white/5">
                {a.kind === "email" ? (
                  <Icon.mail className="h-4 w-4" />
                ) : (
                  <Icon.calendar className="h-4 w-4" />
                )}
              </span>
              <div className="min-w-0 flex-1">
                <div className="truncate text-sm text-zinc-200">{summarize(a)}</div>
                {a.error && (
                  <div className="truncate text-xs text-rose-300">{a.error}</div>
                )}
              </div>
              <span className="hidden font-mono text-xs text-zinc-500 sm:block">
                {fmtTime(a.created_at)}
              </span>
              <StatusPill status={a.status} />
            </li>
          ))}
        </ul>
      ) : (
        <div className="mt-3 text-sm text-zinc-500">
          Nothing yet — connect Google and try the command box above.
        </div>
      )}
    </Card>
  );
}

export default function Settings() {
  return (
    <div className="space-y-5">
      <p className="text-sm text-zinc-500">
        Connect accounts, teach the assistant who your people are, and fire
        off cloud actions — all without touching the phone.
      </p>
      <Stagger className="grid gap-4 lg:grid-cols-2" gap={0.06}>
        <StaggerItem>
          <GoogleCard />
        </StaggerItem>
        <StaggerItem>
          <ContactsCard />
        </StaggerItem>
        <StaggerItem>
          <CommandCard />
        </StaggerItem>
        <StaggerItem>
          <CloudActionsCard />
        </StaggerItem>
      </Stagger>
    </div>
  );
}
