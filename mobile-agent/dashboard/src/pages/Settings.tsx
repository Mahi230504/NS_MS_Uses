import { useEffect, useState } from "react";
import {
  CloudActionRow,
  ContactRow,
  GoogleStatus,
  apiDelete,
  apiPost,
  useApi,
} from "../api";
import { Card, Spinner, fmtTime } from "../components/bits";

function StatusPill({ status }: { status: string }) {
  const cls =
    status === "ok"
      ? "bg-emerald-500/15 text-emerald-300 ring-emerald-400/30"
      : "bg-red-500/15 text-red-300 ring-red-400/30";
  return (
    <span
      className={`inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium ring-1 ${cls}`}
    >
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
      <div className="text-sm font-medium text-zinc-100">Google account</div>
      <p className="mt-1 text-xs text-zinc-500">
        Powers cloud actions — Gmail sends and Google Meet invites, no phone needed.
      </p>
      {banner && (
        <div
          className={
            "mt-3 rounded-lg p-3 text-sm " +
            (banner === "connected"
              ? "bg-emerald-500/10 text-emerald-200"
              : "bg-red-500/10 text-red-200")
          }
        >
          {banner === "connected"
            ? "Google connected — you're all set."
            : "Google connection failed — try again."}
        </div>
      )}
      {loading ? (
        <div className="mt-4">
          <Spinner />
        </div>
      ) : !data ? null : !data.configured ? (
        <div className="mt-4 text-sm text-zinc-400">
          Not configured. Set <code className="rounded bg-white/5 px-1.5 py-0.5">GOOGLE_CLIENT_ID</code>{" "}
          and <code className="rounded bg-white/5 px-1.5 py-0.5">GOOGLE_CLIENT_SECRET</code> in the
          bot's <code className="rounded bg-white/5 px-1.5 py-0.5">.env</code>, then restart.
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
                className="rounded-full bg-white/5 px-2 py-0.5 text-[10px] text-zinc-400 ring-1 ring-white/10"
              >
                {s.replace("https://www.googleapis.com/auth/", "")}
              </span>
            ))}
          </div>
          {data.connected_at && (
            <div className="text-xs text-zinc-500">connected {fmtTime(data.connected_at)}</div>
          )}
          <button
            disabled={busy}
            onClick={disconnect}
            className="rounded-lg bg-red-500/20 px-3 py-2 text-sm font-medium text-red-200 ring-1 ring-red-400/40 hover:bg-red-500/30 disabled:opacity-50"
          >
            Disconnect
          </button>
        </div>
      ) : (
        <button
          disabled={busy}
          onClick={connect}
          className="mt-4 rounded-lg bg-cyan-500/90 px-3 py-2 text-sm font-medium text-black hover:bg-cyan-400 disabled:opacity-50"
        >
          Connect Google
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
      <div className="text-sm font-medium text-zinc-100">Contacts</div>
      <p className="mt-1 text-xs text-zinc-500">
        Names the assistant can resolve by voice — "meet with Ayush" works once
        Ayush is here.
      </p>
      <div className="mt-3 flex gap-2">
        <input
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="name"
          className="w-28 rounded-lg bg-black/40 px-3 py-2 text-sm text-zinc-100 ring-1 ring-white/10 outline-none focus:ring-cyan-400/40"
        />
        <input
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && add()}
          placeholder="email@example.com"
          className="min-w-0 flex-1 rounded-lg bg-black/40 px-3 py-2 text-sm text-zinc-100 ring-1 ring-white/10 outline-none focus:ring-cyan-400/40"
        />
        <button
          disabled={busy || !name.trim() || !email.trim()}
          onClick={add}
          className="rounded-lg bg-cyan-500/90 px-3 py-2 text-sm font-medium text-black hover:bg-cyan-400 disabled:opacity-50"
        >
          Add
        </button>
      </div>
      {loading ? (
        <div className="mt-4">
          <Spinner />
        </div>
      ) : data && data.items.length ? (
        <ul className="mt-3 divide-y divide-white/5">
          {data.items.map((c) => (
            <li key={c.id} className="flex items-center gap-3 py-2">
              <span className="text-sm text-zinc-200">{c.name}</span>
              <span className="min-w-0 flex-1 truncate text-xs text-zinc-500">{c.email}</span>
              <button
                disabled={busy}
                onClick={() => remove(c.name)}
                className="rounded px-2 py-0.5 text-xs text-zinc-500 hover:bg-red-500/10 hover:text-red-300 disabled:opacity-50"
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
      <div className="text-sm font-medium text-zinc-100">Command</div>
      <p className="mt-1 text-xs text-zinc-500">
        Type anything you'd say to the assistant — "email alice@x.com that the
        demo is ready", "meet with Ayush tomorrow 3pm".
      </p>
      <div className="mt-3 flex gap-2">
        <input
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && run()}
          placeholder="what should Atlas do?"
          className="min-w-0 flex-1 rounded-lg bg-black/40 px-3 py-2 text-sm text-zinc-100 ring-1 ring-white/10 outline-none focus:ring-cyan-400/40"
        />
        <button
          disabled={busy || !text.trim()}
          onClick={run}
          className="rounded-lg bg-cyan-500/90 px-3 py-2 text-sm font-medium text-black hover:bg-cyan-400 disabled:opacity-50"
        >
          {busy ? "Running…" : "Run"}
        </button>
      </div>
      {reply && (
        <div className="mt-3 rounded-lg bg-white/5 p-3 text-sm text-zinc-300">{reply}</div>
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
      <div className="text-sm font-medium text-zinc-100">Recent cloud actions</div>
      <p className="mt-1 text-xs text-zinc-500">
        Emails sent and meetings scheduled straight from the cloud — no phone.
      </p>
      {loading ? (
        <div className="mt-4">
          <Spinner />
        </div>
      ) : data && data.items.length ? (
        <ul className="mt-3 divide-y divide-white/5">
          {data.items.map((a) => (
            <li key={a.id} className="flex items-center gap-3 py-2.5">
              <span className="text-lg">{a.kind === "email" ? "✉" : "📅"}</span>
              <div className="min-w-0 flex-1">
                <div className="truncate text-sm text-zinc-200">{summarize(a)}</div>
                {a.error && (
                  <div className="truncate text-xs text-red-300">{a.error}</div>
                )}
              </div>
              <span className="hidden text-xs text-zinc-500 sm:block">
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
      <h1 className="text-xl font-semibold text-zinc-100">Settings</h1>
      <div className="grid gap-4 lg:grid-cols-2">
        <GoogleCard />
        <ContactsCard />
        <CommandCard />
        <CloudActionsCard />
      </div>
    </div>
  );
}
