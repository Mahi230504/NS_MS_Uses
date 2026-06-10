# Atlas dashboard

A live personal-assistant web view for the mobile-agent: watch a task run in
real time (the agent's screen + the taps it makes, drawn on top), browse your
history per app, and see your saved tasks, schedules, comparisons, and
most-used apps.

It talks to the in-process dashboard API (`bot/dashboard_api.py`), which runs
inside the bot. The dashboard is **view-only** — approvals and control stay on
Telegram.

## Configure

In the bot's `.env`:

```
DASHBOARD_TOKEN=<a secret>      # enables the dashboard server
DASHBOARD_PORT=8770             # default
```

Generate a token: `python -c "import secrets;print(secrets.token_urlsafe(24))"`

## Dev (hot reload)

```bash
cd dashboard
npm install
npm run dev            # http://localhost:5173  (proxies /api + /events -> :8770)
```

Start the bot too (so the API is up). Open the dev URL, paste the
`DASHBOARD_TOKEN` when prompted.

## Production (one process)

```bash
cd dashboard
npm install
npm run build          # -> dashboard/dist
```

The bot serves the built bundle directly: open `http://127.0.0.1:8770`. For an
on-device view, the bot `adb reverse`s the port, so the phone's browser can
reach the same URL over USB. Nothing is exposed on the network.

## Stack

React + Vite + TypeScript + Tailwind. State: a small Zustand store fed by the
`/events` SSE stream (`src/live.ts`); REST via a tiny `useApi` hook
(`src/api.ts`). The screenshot + tapped-path overlay is `src/components/DeviceCanvas.tsx`.
