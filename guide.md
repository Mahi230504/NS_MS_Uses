# Masterclass Setup Guide — Running the Mobile Agent E2E on Your Own Phone

This guide is for the **instructor presenting the masterclass**. It takes you from a
clean machine + a personal Android phone to a working end-to-end demo (voice/text →
agent drives a real app → live dashboard).

> **Mental model:** your **laptop runs everything** (the bot, the AI calls, the
> dashboard). The **phone is just the screen and hands** — it's driven over a USB
> cable via `adb`. Nothing is exposed to the internet; the phone talks to the laptop
> over the cable. So "running on his phone" really means *"his laptop drives his
> phone."*

---

## 0. Can it run on any phone?

**Yes — almost any Android phone.** The agent is app-agnostic and
resolution-agnostic: it reads the screen (screenshot + UI tree) and taps live
coordinates, so there is **no per-phone or per-app tuning**. The only thing tied to a
specific phone is one line in a config file (the device serial), which you'll set in
Step 5.

**Requirements / caveats:**
- **Android phone** (not iOS — the iOS path is a stub). Android 10+ recommended;
  tested on Android 14.
- **USB cable** to the laptop (the demo uses a wired connection).
- **Gboard installed** on the phone — the agent restores Gboard as the keyboard after
  each task and the typing path assumes it.
- The agent **cannot pass the lockscreen PIN** — you unlock the phone manually before
  a task on every phone.
- **Always-on background voice** is OEM-dependent and was the hard part on realme/ColorOS.
  For the masterclass you do **not** need it — trigger by **Telegram message** or a
  **tap-to-talk HTTP shortcut**, both of which work everywhere. (See Step 8, optional.)

---

## 1. Install the host tools (on the laptop)

You need these on `PATH`:

| Tool | Version | Check | Get it |
|------|---------|-------|--------|
| Python | 3.14 | `python --version` | python.org / Homebrew |
| `adb` (Android Platform Tools) | any recent | `adb --version` | Android SDK platform-tools |
| Node.js + npm | 18+ (20 tested) | `node --version` | nodejs.org / Homebrew |
| git | any | `git --version` | — |

> macOS quick install: `brew install python@3.14 android-platform-tools node git`

---

## 2. Get the code & Python deps

```bash
git clone <repo-url> NS_MS_Uses
cd NS_MS_Uses/mobile-agent

python -m venv .venv
.venv/bin/pip install -r requirements.txt
```

> **Always launch the bot with `.venv/bin/python`** — a bare system/Homebrew Python
> fails with `ModuleNotFoundError` because the dependencies live only in the venv.

---

## 3. Get the credentials you'll paste into `.env`

You need **two required** credentials and a few optional ones.

### Required

1. **An AI vision/reasoning provider key** — pick ONE:
   - **OpenRouter** (what the dev setup uses): sign up at openrouter.ai → create an API
     key. Model used: `google/gemini-2.5-flash`. *(Recommended — one key, cheap.)*
   - **Google AI Studio (Gemini)**: aistudio.google.com → API key (free tier works).

2. **A Telegram bot** — this is the control plane (status, approvals, OTP/payment gates):
   - In Telegram, message **@BotFather** → `/newbot` → copy the **bot token**.
   - Get **your own numeric Telegram user ID**: message **@userinfobot** → copy the ID.
     This becomes `TELEGRAM_ADMIN_ID` (the owner who can issue commands).

### Optional (only if you'll demo these features)

3. **Google Workspace (Gmail send + Google Meet scheduling)** — only if the demo
   includes the cloud actions. In Google Cloud Console (console.cloud.google.com):
   1. Create/pick a project → enable the **Gmail API** and **Google Calendar API**.
   2. Configure the **OAuth consent screen** (External; add yourself as a test user).
   3. **Credentials → Create credentials → OAuth client ID → Web application.**
   4. Add the authorized redirect URI **exactly**:
      `http://127.0.0.1:8770/api/google/oauth/callback`
   5. Copy the **client ID + secret** (you'll paste them in Step 4, then click
      "Connect Google" on the dashboard Settings page after first launch).

---

## 4. Create the `.env` file

The repo ships a fully self-documenting template. Copy it and edit:

```bash
cp .env.example ~/.mobile-agent/.env   # the bot reads config from ~/.mobile-agent/.env
```

> If your setup keeps `.env` inside the project dir instead, copy it to
> `mobile-agent/.env`. Either works as long as it's the one the bot loads.

Fill in at minimum:

```ini
# --- Vision provider (pick the one matching your key) ---
VISION_PROVIDER=openrouter
OPENROUTER_API_KEY=<your-openrouter-key>
OPENROUTER_MODEL=google/gemini-2.5-flash
# (or: VISION_PROVIDER=gemini + GEMINI_API_KEY=... + GEMINI_MODEL=gemini-2.5-flash)

# --- Telegram ---
TELEGRAM_BOT_TOKEN=<botfather-token>
TELEGRAM_ADMIN_ID=<your-numeric-telegram-id>

# --- Device (set in Step 5 after `adb devices`) ---
ANDROID_DEVICE_ID=<your-phone-serial>
ALLOW_PHYSICAL_DEVICE=1      # REQUIRED to drive a real phone (default refuses)

# --- Webhook (tap-to-talk / curl triggers) ---
WEBHOOK_SECRET=<generate one>     # python -c "import secrets;print(secrets.token_urlsafe(32))"
WEBHOOK_HOST=127.0.0.1
WEBHOOK_PORT=8765

# --- Dashboard ---
DASHBOARD_TOKEN=<generate one>    # python -c "import secrets;print(secrets.token_urlsafe(24))"
DASHBOARD_HOST=127.0.0.1
DASHBOARD_PORT=8770

# --- Misc ---
TIMEZONE=Asia/Kolkata             # your IANA timezone (used by scheduled tasks)
SESSION_TIMEOUT_SECONDS=600

# --- Optional: Google cloud actions ---
GOOGLE_CLIENT_ID=<if using Gmail/Meet>
GOOGLE_CLIENT_SECRET=<if using Gmail/Meet>
```

> **Security:** never commit `.env`; never paste a Google service-account JSON inline.

---

## 5. Connect & register the phone

1. On the phone: **Settings → About phone → tap Build number 7×** to unlock Developer
   Options, then **Developer Options → enable USB debugging**.
2. Plug the phone into the laptop via USB.
3. On the phone, accept the **"Allow USB debugging?"** RSA prompt (tick "always allow").
4. On the laptop:
   ```bash
   adb devices
   ```
   You should see a line like `R58A1234567   device`. Copy that serial into
   `ANDROID_DEVICE_ID` in `.env`.

   - If it says `unauthorized` → re-accept the prompt on the phone.
   - If nothing shows → try a different cable/port (some cables are charge-only).

5. **Install and sign into the apps** you'll demo (Blinkit, Swiggy, Zomato, etc.).
   The agent drives apps that are **already logged in** — it does not handle first-run
   login/OTP well. Also install **Gboard** and set it as the keyboard.

---

## 6. Build the dashboard (one time)

```bash
cd dashboard
npm install
npm run build      # produces dashboard/dist/, served by the bot at :8770
cd ..
```

> You only rebuild when the dashboard UI changes. The bot serves `dist/` from disk, so
> UI updates go live without a bot restart.

---

## 7. Launch & verify

From `mobile-agent/`:

```bash
# 1. Make sure the phone is plugged in and UNLOCKED
adb devices                       # confirm your serial is listed

# 2. Start the bot (serves webhook :8765 AND dashboard :8770)
.venv/bin/python -u main.py > /tmp/atlas-bot.log 2>&1 &

# 3. The bot sets adb reverse on startup, but RE-RUN it after any reconnect:
adb reverse tcp:8765 tcp:8765
adb reverse tcp:8770 tcp:8770

# 4. Health checks
curl -s http://127.0.0.1:8765/health        # {"ok": true}  (webhook)
curl -s http://127.0.0.1:8770/api/health     # {"ok": true}  (dashboard)
```

Open the **dashboard** in a browser: `http://127.0.0.1:8770` and paste the
`DASHBOARD_TOKEN` when prompted.

If you enabled Google: go to the dashboard **Settings → Connect Google** and complete
the OAuth flow once.

**Smoke test without Telegram:** `.venv/bin/python test_local.py`

---

## 8. Trigger a task (three ways)

Pick whichever fits the demo. **Telegram and curl work on every phone** — use them for
the masterclass. The on-device voice trigger is optional and OEM-dependent.

**A. Telegram (simplest, most reliable):** message your bot, e.g.
`order milk on blinkit`. The bot replies with what it understood; confirm, and it runs.
Approvals / payment / OTP gates appear as Telegram buttons.

**B. curl / HTTP shortcut (tap-to-talk):**
```bash
curl -X POST http://127.0.0.1:8765/trigger \
  -H "X-Webhook-Secret: $WEBHOOK_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"text": "order milk on blinkit", "autorun": "true"}'
```
On the phone, the free **HTTP Shortcuts** app can send this same POST from a home-screen
button (paste the secret as a header). This is the recommended "press-to-talk" path.

**C. Full hands-free voice (optional, advanced):** Tasker + AutoVoice capture spoken
text and POST it to `/trigger`. This is the part that fights OEM background-mic limits
(realme/ColorOS clamps it); a Pixel/Samsung may be easier. **Skip this for the
masterclass unless you specifically want always-on voice.**

---

## 9. Live-demo checklist (run through this before going on stage)

- [ ] Phone plugged in, **unlocked**, screen-timeout set long (Settings → Display).
- [ ] `adb devices` shows the serial; `adb reverse --list` shows both 8765 + 8770.
- [ ] Both health endpoints return `{"ok": true}`.
- [ ] Dashboard loads at `:8770` with the token.
- [ ] Demo apps installed **and logged in**; Gboard is the active keyboard.
- [ ] **Stable Wi-Fi** (not a flaky phone hotspot) — the agent calls the AI provider on
      every step; a dropped connection looks like a bug.
- [ ] Do one **warm-up run** before the audience — the first AI call after idle can be
      slow (cold start); warm it's ~2.5s/step.
- [ ] Speak app names clearly if using voice — mis-transcription (e.g. "blanket" for
      "Blinkit") sends the agent to the wrong target and it aborts safely.

---

## 10. Quick troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `RuntimeError: Expected device '…' is not attached` | wrong/missing serial, phone unplugged or locked-out of adb | `adb devices`; fix `ANDROID_DEVICE_ID`; replug; accept RSA prompt |
| Bot exits with `ModuleNotFoundError` | launched with system Python | relaunch with `.venv/bin/python` |
| Phone-side trigger / dashboard unreachable from phone | adb reverse dropped on reconnect | re-run both `adb reverse tcp:8765…` and `tcp:8770…` |
| Agent stalls on a PIN screen | can't pass lockscreen | unlock the phone manually before the task |
| Task aborts "detected N action loops" | wrong app/screen (often a mis-transcribed command) or a popup shadowing the UI | re-issue clean text; dismiss popups; confirm the right app is logged in |
| "offline" badge on dashboard / Telegram NetworkError | flaky network | switch to stable Wi-Fi (auto-recovers) |
| Typing goes wrong / weird keyboard | non-Gboard IME | install Gboard; the agent restores it after tasks |

---

## 11. One-line restart (for during the session)

```bash
pkill -f main.py; sleep 2; \
.venv/bin/python -u main.py > /tmp/atlas-bot.log 2>&1 & \
sleep 6; adb reverse tcp:8765 tcp:8765; adb reverse tcp:8770 tcp:8770; \
curl -s http://127.0.0.1:8770/api/health
```

---

**Bottom line for the instructor:** install host tools → clone + venv → paste an AI key
and a Telegram bot token into `.env` → plug in his phone, set its serial + `ALLOW_PHYSICAL_DEVICE=1`
→ build the dashboard → launch → trigger from Telegram. The phone itself needs nothing
but USB debugging, the demo apps logged in, and Gboard. Voice is optional.
