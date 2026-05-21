# mobile-agent — generalized mobile computer use agent

## What this is
A production-grade, generalized AI agent that controls any Android app via computer use (vision + ADB), operated through Telegram. Works on any app — Blinkit, Swiggy, Zomato, Uber, anything — without any app-specific API or scraping.

## Core architecture
- **Interface**: Telegram bot (python-telegram-bot v20, async)
- **Vision + reasoning**: pluggable `VisionProvider` (default: Gemini 2.0 Flash via `google-genai`, free-tier compatible)
- **Device control**: ADB (Android Debug Bridge) wrapping adb shell commands
- **Agent loop**: Custom async state machine (no LangGraph dependency)
- **Security**: HITL gates, action validator, audit logger, free-tier RPM/RPD throttle

## Project structure

```
mobile-agent/
├── CLAUDE.md
├── .env.example
├── requirements.txt
├── main.py
├── test_local.py             # Standalone smoke test (no Telegram)
├── bot/
│   ├── __init__.py
│   ├── telegram_bot.py       # Bot init, polling setup
│   └── handlers.py           # /start, /status, /abort, message, approval callback
├── agent/
│   ├── __init__.py
│   ├── orchestrator.py       # Master agent: takes a VisionProvider, runs the loop
│   ├── computer_use.py       # Shim — delegates to configured VisionProvider
│   ├── action_executor.py    # Translates action dict → ADB commands
│   ├── state_machine.py      # Task + TaskState (IDLE..TIMED_OUT)
│   └── providers/            # Pluggable vision/reasoning providers
│       ├── __init__.py       # Factory: make_provider("gemini", ...)
│       ├── base.py           # VisionProvider Protocol + RequestUsage/ProviderResponse
│       ├── gemini.py         # Google Gemini implementation (google-genai)
│       ├── throttle.py       # Free-tier RPM/RPD limiter
│       └── _parse.py         # JSON-object extraction from model output
├── security/
│   ├── __init__.py
│   ├── hitl_gate.py          # Detects irreversible actions, pauses loop, awaits Telegram approval
│   ├── action_validator.py   # Whitelist safe actions, block dangerous ones
│   └── audit_logger.py       # Structured JSON log of every action taken
├── device/
│   ├── __init__.py
│   ├── adb_controller.py     # tap, swipe, type, screencap, key_event wrappers
│   └── emulator.py           # Check emulator is running, get device ID
└── config/
    ├── __init__.py
    ├── settings.py           # Loads from .env, single Settings dataclass
    └── prompts.py            # System prompt for the vision agent (provider-agnostic)
```

## Security rules (non-negotiable, enforce in every file)
1. **Zero hardcoded secrets** — everything from `.env` via `python-dotenv`.
2. **HITL gate fires on ANY of**: payment screens, delete/uninstall actions, permission dialogs, OTP entry.
3. **`action_validator.py` has an explicit `BLOCKED_ACTIONS` list**: no calls, no SMS send, no settings/factory-reset.
4. **Every ADB command is logged to `audit_logger` before execution**, never after.
5. **Telegram user whitelist** — only approved user IDs can issue commands (from `.env`).
6. **Session timeout**: if a task runs > 3 minutes without completing, auto-abort and notify the user.
7. **Emulator only** — detect if a real physical device is connected and REFUSE to run (safety).

## The agent loop (implement in `orchestrator.py`)

```
receive_task(text)
  → for each step (max 30):
      screenshot()                          # adb screencap
      → vision.get_next_action()            # provider → parsed action dict + usage
      → validate_action()                   # whitelist + blocklist
      → hitl_check()                        # pause if irreversible
      → audit_log(action, "EXECUTING")      # rule 4: BEFORE execute
      → execute_action()                    # adb tap/type/swipe (or terminal: done)
      → audit_log(action, "RESULT: …")      # outcome
  → report_done()                           # final summary to Telegram
```

Session-level guards: `asyncio.wait_for(..., timeout=SESSION_TIMEOUT_SECONDS)` wraps the whole loop; `MAX_LOOP_ITERATIONS = 30` caps step count.

## Design principles
- HITL gates on ALL irreversible actions (payment / OTP / delete / permission)
- Emulator-only enforcement — REFUSE if any physical device is attached
- Provider-agnostic vision layer (swap Gemini for Claude / GPT-4V via the `VisionProvider` Protocol)
- Full audit log (JSON, daily rotation, key + content redaction)
- Action validator with explicit allow-set + block-set
- Free-tier safe: per-provider RPM/RPD throttle, daily-quota raises `QuotaExceeded` instead of silent failure
- Telegram user whitelist
- Session timeout with auto-abort
- Clean async architecture (no threading hacks)
- API key never touches disk or logs
