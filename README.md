# mobile-agent — a generalized mobile computer-use agent

**Drive any Android app by voice or chat, using vision + ADB, with human approval on anything irreversible.**

`mobile-agent` (wake word: *"Atlas"*) is a production-grade agent that operates a real phone the way a person would — it looks at the screen, reads the accessibility tree, decides the next gesture with a vision-language model, and taps/types/swipes over ADB. No per-app API, no scraping, no accessibility-service hacks. It works on Blinkit, Swiggy, Uber, Amazon, WhatsApp — anything installed — and it pauses for you before paying, entering an OTP, deleting, or granting a permission.

You talk to it through **Telegram** (text or voice notes) or trigger it **hands-free by voice** through an on-device HTTP shortcut. Every action is validated, gated, and audit-logged.

---

## Table of contents

1. [What makes it different](#1-what-makes-it-different)
2. [Tech stack](#2-tech-stack)
3. [Architecture at a glance](#3-architecture-at-a-glance)
4. [End-to-end flow](#4-end-to-end-flow) ← the core walkthrough
5. [One agent step, in detail](#5-one-agent-step-in-detail)
6. [Component reference](#6-component-reference) ← every module + its functions
7. [The agent loop internals](#7-the-agent-loop-internals)
8. [Safety model](#8-safety-model)
9. [Configuration reference](#9-configuration-reference)
10. [Setup & run](#10-setup--run)
11. [Project layout](#11-project-layout)
12. [Known limitations & design notes](#12-known-limitations--design-notes)

---

## 1. What makes it different

- **Generalized, not scripted.** The agent has no hard-coded flows. It reasons over a screenshot + the live UI tree every step, so the same loop drives a grocery app, a cab app, or a chat app.
- **Two front doors.** A full Telegram bot (menus, free text, voice notes) *and* a zero-UI voice trigger: press a phone shortcut, speak, confirm, done.
- **Grounded perception.** It doesn't guess pixel coordinates. `uiautomator` gives it a compacted element list with real center coordinates, action-button labels tied to product cards, and screen-role tags (`[ACTION]`, `[CART]`, `[LOCATION]`, `[CATEGORY?]`).
- **Defensive by construction.** A whitelist/blocklist validator, a human-in-the-loop (HITL) gate on every sensitive screen, a per-user permission policy, an emulator-only default, and a redacted JSON audit trail.
- **Self-correcting.** Nine structural guards reject hallucinated or repetitive actions *before* they touch the device; perceptual-hash dedup and loop detection break the agent out of dead ends.
- **Pluggable brains.** The vision layer is a `Protocol` — Gemini (AI Studio), Vertex AI, or any OpenRouter model — swappable via one env var.
- **Cross-platform device layer.** Android (ADB) and iOS (`idb`) implement the same `DeviceController` protocol.

---

## 2. Tech stack

| Concern | Choice |
|---|---|
| Language / runtime | Python 3.14, fully `async` |
| Chat interface | `python-telegram-bot` v20 (async) |
| Voice / external trigger | `aiohttp` HTTP server (`POST /trigger`) |
| Vision + reasoning | `google-genai` (Gemini/Vertex) **or** `openai` SDK → OpenRouter |
| Device control | ADB (`adb` CLI) for Android; `idb` for iOS |
| Screen dedup | `imagehash` + `Pillow` (perceptual hash) |
| Persistence | `aiosqlite` (task + step history) |
| Config | `python-dotenv` |
| Tests | `pytest`, `pytest-asyncio`, `pytest-mock` |

There is **no LangGraph / agent framework** — the loop is a hand-written async state machine.

---

## 3. Architecture at a glance

```
                         ┌──────────────────────────────────────────────────────────┐
   USER                  │                       HOST (laptop)                        │
  ┌──────┐  text/voice   │  ┌────────────┐                                            │
  │Telegram│◀───────────▶│  │  Handlers  │  bot/handlers.py                           │
  └──────┘   approve/deny│  │ (PTB glue) │                                            │
                         │  └─────┬──────┘                                            │
  ┌──────────┐  POST     │        │            ┌──────────┐  intent JSON              │
  │ Phone    │ /trigger  │  ┌─────▼──────┐     │  Router  │  bot/router.py ───┐       │
  │ HTTP     │──USB/adb──▶│  │  Webhook   │────▶│ + Apps   │  bot/apps.py      │       │
  │ Shortcut │  reverse  │  │ aiohttp    │     └──────────┘                   │       │
  └──────────┘           │  └────────────┘            rendered task string    │       │
                         │        │                          │                │       │
                         │        ▼                          ▼                ▼       │
                         │  ┌──────────────────────────────────────────────────────┐ │
                         │  │             Orchestrator  (agent/orchestrator.py)      │ │
                         │  │   screencap → tree → vision → validate → guards →      │ │
                         │  │   HITL gate → execute → audit → verify (loop ≤30)      │ │
                         │  └───┬───────────┬───────────┬──────────┬──────────┬─────┘ │
                         │      │           │           │          │          │       │
                         │  ┌───▼───┐  ┌────▼────┐  ┌───▼───┐  ┌───▼────┐  ┌──▼─────┐ │
                         │  │Vision │  │ UI tree │  │ HITL  │  │ Audit  │  │ Persist│ │
                         │  │provider│ │ + Skills│  │ gate  │  │ logger │  │(sqlite)│ │
                         │  │+Throttle│ │+Prompts│  │+Valid.│  └────────┘  └────────┘ │
                         │  └───┬────┘  └─────────┘  └───────┘                        │
                         │      │  HTTPS                                              │
                         │      ▼                  ┌──────────────────────────┐       │
                         │  ☁ Gemini/OpenRouter    │  AdbController            │       │
                         │                         │  device/adb_controller.py │──────┼──▶ 📱 phone
                         │                         │  tap/swipe/type/screencap │ ADB   │   (or emulator)
                         │                         │  dump_ui_xml/launch       │       │
                         │                         └──────────────────────────┘       │
                         └──────────────────────────────────────────────────────────┘
```

---

## 4. End-to-end flow

A task travels through the system in three stages: **enter → route → run**. Here is the full path from a spoken word to a completed order.

### 4.1 Startup wiring (`main.py`)

On launch, `main()` constructs and connects every singleton in this order:

```
load_settings()                       # config/settings.py → frozen Settings from .env
  → check_emulator_running(allow_physical)    # device/emulator.py — refuse unknown/forbidden devices
  → AdbController(device_id)                   # device handle; force_off_adbkeyboard() to clean prior run
  → HitlGate()                                 # approval gate (asyncio.Event registry)
  → AuditLogger(log_dir)                       # rotating JSON audit log
  → make_provider(vision_provider, …)          # agent/providers — Gemini | Vertex | OpenRouter
  → UserStore(users_path) + _bootstrap_users() # who may trigger (JSON-backed)
  → PairCodeIssuer()                           # /pair onboarding codes
  → SkillRegistry(skills/)                      # per-app prompt hints
  → TaskRepository(db_path).initialize()        # aiosqlite
        .recover_orphans()                      # mark tasks left RUNNING by a crash as FAILED
  → Orchestrator(adb, hitl, audit, vision, users, skills, repo, …)
  → Router(vision)                              # ONLY if the provider implements complete_text (OpenRouter does)
  → build_application(token)  +  Handlers(…)    # PTB app + glue
  → orchestrator.on_status_update    = handlers.on_status_update    # callbacks back to Telegram
  → orchestrator.on_approval_request = handlers.on_approval_request
  → register_handlers(app, handlers)
  → _wire_webhook(…)                            # start aiohttp /trigger server IF WEBHOOK_SECRET set
  → app.run_polling()                           # block; PTB owns the event loop
```

> The intent **Router is only active when the configured provider exposes `complete_text`** — currently only `OpenRouterProvider`. With the Gemini/Vertex providers, routing is skipped and every message goes through the freeform path (the vision model figures everything out from scratch).

### 4.2 Entry path A — Telegram message

```
Telegram update
  → Handlers.message(update, context)
      → UserStore.is_allowed(user_id)                  # auth gate (first thing, always)
      → if session is AWAITING_PARAM:  fill the pending template param
                                       → _launch_task_from_session(...)
      → elif Router configured:        route = await Router.route(text)
                                       → if route:  _launch_route(...)   (may ask for a param first)
                                       → else:      _launch_freeform_task(...)
      → else:                          _launch_freeform_task(...)
  ─────────────────────────────────────────────────────────────────────
  each _launch_* builds a Task and does:
      asyncio.create_task( Orchestrator.run_task(task, launch_package=<pkg|None>) )
```

Voice notes are STT-transcribed by Telegram/the client and arrive as text on the same path. Menu-driven use (`/start` → app picker → task picker) is handled by `bot/session.py` + `bot/apps.py` and ends at the same `_launch_task_from_session`.

### 4.3 Entry path B — hands-free voice (the webhook)

A phone shortcut (e.g. the free *HTTP Shortcuts* app) captures speech-to-text and `POST`s it to the host. For a USB phone the host runs `adb reverse tcp:8765 tcp:8765` at startup, so the phone reaches `http://127.0.0.1:8765` **over the cable** — nothing is exposed on the network.

```
POST /trigger      (header X-Webhook-Secret: <WEBHOOK_SECRET>, body {"text": "...", "autorun"?: "true", "confirm"?: ...})
  → webhook.trigger(request)
      → hmac.compare_digest(secret)                    # timing-safe auth; wrong/empty secret → 401
      → body has "confirm":   handle_external_confirm(owner, approve)   # phase 2 of confirm flow
      → body autorun truthy:  handle_external_run(owner, text)          # single-shot: trigger + auto-confirm
      → otherwise:            handle_external_trigger(owner, text)      # phase 1: understand, ask "Confirm?"
```

The **confirm-first** flow (default, safest):
1. **Phase 1** — `handle_external_trigger`: `_strip_wake_word(text)` removes a leading "okay atlas / hey atlas / atlas", the Router turns it into a structured intent, it's stashed in `_pending[user]` (90 s TTL), and the agent **speaks back** *"Got it — add milk on Blinkit. Confirm?"*.
2. **Phase 2** — `handle_external_confirm(approve=True)`: pops the pending intent, checks the TTL, and fires `Orchestrator.run_task`.

`autorun=true` collapses both phases into one call (phase 1 then auto-confirm) for trusted shortcuts.

### 4.4 Routing (`bot/router.py` + `bot/apps.py`)

The Router is an **LLM intent classifier over a fixed catalogue**. `bot/apps.py` is a static registry of **23 apps across 7 categories** (groceries, food, mobility, shopping, media, tools, payments), each with canned `TaskTemplate`s. `_registry_listing()` serializes every `(app_id, task_id, label)` into the prompt, and the model must answer with **only**:

```json
{"app_id": "blinkit", "task_id": "order", "param": "milk"}
```

`_parse_response` validates those ids against the live registry (`get_app`/`get_task`) — a hallucinated id resolves to `None` and safely falls back to freeform. Alias resolution ("blinkit" → `com.grofers.customerapp`, "cab" → `uber`) is done entirely by the LLM; there is no hard-coded alias map. The handoff to the orchestrator is a **rendered plain-English string**:

```
Route(app=blinkit, task=order, param="milk")
  → apps.render_prompt("Add {param} to the cart and proceed to checkout.", "milk")
  → "Add milk to the cart and proceed to checkout."   ← this string becomes Task.description
```

### 4.5 Execution — the orchestrator sequence

```
Orchestrator.run_task(task, launch_package)
  │  reset per-task counters; switch IME → ADBKeyboard (reliable typing on OEM ROMs)
  │  if launch_package: adb monkey -p <pkg> … LAUNCHER 1
  │  wrap the loop in asyncio.wait_for(timeout = SESSION_TIMEOUT_SECONDS)
  ▼
  for step in 1..MAX_LOOP_ITERATIONS(30):
        screencap ──▶ perceptual hash ──▶ (unchanged & budget left? → synthetic "wait", skip the model)
        skill hint  = SkillRegistry.get(foreground_package)
        ui_tree     = adb.dump_ui_xml() → ui_tree.to_prompt_section(xml)
        loop_hint   = repetition detector
        action,usage = vision.get_next_action(screenshot, task, history, screen_size, skill_hint, ui_tree)
        validate(action)                       # security/action_validator.py — hard fail if blocked
        guard stack (9 checks)                 # soft-reject → re-prompt next step
        gate_with_hitl(action)                 # pause + await Telegram approve/deny on sensitive screens
        if action == "done":  state=DONE; report; return
        result = execute_with_retry(action)    # action_executor → AdbController  (≤4 attempts)
        audit RESULT; persist step; verify the screen actually changed
  ▼
  finally: restore the original IME; persist terminal state; notify Telegram
```

Throughout, the orchestrator pushes messages back to the user via two callbacks set in `main.py`:
- `on_status_update(task, msg)` → throttled per-step progress + lifecycle ("starting", "done", "failed", "timeout").
- `on_approval_request(task, action)` → the **Approve / Deny** inline-button card for HITL.

When you tap a button, `Handlers.menu_callback → _handle_approval_callback` calls `HitlGate.grant()` / `.deny()`, which sets the `asyncio.Event` the orchestrator is awaiting — and the loop resumes or fails with "user denied approval".

---

## 5. One agent step, in detail

Each loop iteration is logged with these exact phase names (you'll see them in the logs):

| Phase | What happens | Code |
|---|---|---|
| `begin` | increment `task.step_count` | `_loop` |
| `screencap` | `adb exec-out screencap -p` → PNG bytes; compute perceptual hash | `AdbController.screencap`, `phash.compute` |
| *(dedup)* | if this screen's hash == last screen the model saw, and we haven't exhausted 3 consecutive synthetic waits → inject `{"action":"wait"}` and **skip the model call** | `_maybe_synthesize_wait` |
| `skill+ui_tree` | load per-app skill markdown; `dump_ui_xml()` → parse → compact element listing; compute loop hint | `_lookup_skill`, `_lookup_ui_tree`, `_loop_hint` |
| `vision call` | send system prompt + skill + screenshot + UI tree + task + history to the model | `VisionProvider.get_next_action` |
| `vision returned` | parsed action dict in hand; usage tokens accumulated; step artifacts written (`step_NN.png/.xml/.json/.ui_prompt.txt`) | — |
| `validate` | normalize aliases (`click`→`tap`), coerce coords, allow/block scan; **block = hard fail** | `action_validator.validate` |
| *(guard stack)* | 9 structural rejections (see §7.1); a reject appends a hint to history and re-prompts next step | `_coords_mismatch`, `_intent_mismatch`, … |
| `hitl gate` | sensitivity check (keywords + optional vision); pause + await approval on sensitive screens | `_gate_with_hitl`, `HitlGate` |
| `execute` | audit `EXECUTING` **before** acting; dispatch to the device with retry/backoff | `action_executor.execute`, `_execute_with_retry` |
| `audit` | audit `RESULT: …`; append to `task.history`; persist step | `AuditLogger.log_action`, `TaskRepository.append_step` |
| *(verify)* | re-screencap; if the hash didn't change after a tap/type/swipe, escalate: back-button at streak 2, fail at streak 3 | `_verify_outcome` |

---

## 6. Component reference

### 6.1 Entry & interface — `main.py`, `bot/`

| File | Purpose |
|---|---|
| `main.py` | Process entry point; builds and wires every singleton; starts PTB polling and (optionally) the webhook server. |
| `bot/telegram_bot.py` | PTB `Application` factory (`build_application`) and `register_handlers` — binds commands, the message handler, and the callback-query handler. |
| `bot/handlers.py` | All Telegram callbacks + the bridge from the webhook to the orchestrator. |
| `bot/webhook.py` | aiohttp app: `POST /trigger` (authenticated) and `GET /health`. |
| `bot/router.py` | LLM intent classifier → `Route(app, task, param)` or `None`. |
| `bot/apps.py` | Static registry of 23 apps and their task templates. |
| `bot/session.py` | In-memory per-user menu navigation state (`IDLE`/`CHOOSING_APP`/`CHOOSING_TASK`/`AWAITING_PARAM`/`RUNNING`). |
| `bot/users.py` | JSON-backed authorization store + per-user policy. |
| `bot/pairing.py` | One-outstanding, 5-minute TTL pair-code issuer for onboarding. |

**`bot/handlers.py` — key methods**

| Signature | Responsibility |
|---|---|
| `Handlers.__init__(application, orchestrator, hitl, users, pairing, admin_id, repo, router)` | Stores service refs; inits session store, `_running`, `_pending`. |
| `async start / status / abort / history` | Slash commands: app picker, current task, cancel running `asyncio.Task`, recent runs from sqlite. |
| `async pair / issue_pair_code / list_users / revoke` | Onboarding + admin user management. |
| `async message(update, ctx)` | Routes free text: param-fill → router → freeform. |
| `async menu_callback(update, ctx)` | Catch-all callback handler; dispatches `approve`/`deny` + menu nav. |
| `async _launch_task_from_session / _launch_freeform_task / _launch_route` | Build a `Task` and `create_task(orchestrator.run_task(...))`. |
| `async handle_external_trigger / _confirm / _run` | The voice webhook's understand → confirm → run phases. |
| `async on_status_update(task, msg)` / `on_approval_request(task, action)` | Orchestrator → Telegram callbacks (status text; Approve/Deny card). |
| `_strip_wake_word(text)` | Strip leading "okay atlas / hey atlas / atlas" (word-boundary safe). |

### 6.2 Orchestrator core — `agent/orchestrator.py`, `state_machine.py`, `phash.py`, `computer_use.py`

| File | Purpose |
|---|---|
| `agent/orchestrator.py` | The master agent loop (~1,900 lines): perception → reasoning → guards → HITL → execution → verification, plus all reporting/persistence. |
| `agent/state_machine.py` | `TaskState` enum + the `Task` dataclass holding all per-run state. |
| `agent/phash.py` | `compute(image_bytes) -> str` — 64-bit perceptual hash hex; used for dedup and outcome verification. |
| `agent/computer_use.py` | Thin back-compat shim exposing module-level `get_next_action` over the default provider (used by `test_local.py`). |

**Orchestrator — selected methods** (full guard list in §7.1)

| Signature | Responsibility |
|---|---|
| `run_task(task, *, launch_package) -> Task` | Public entry: reset state, set IME, optionally launch the app, run the loop under `asyncio.wait_for`, handle terminal exceptions, restore IME. |
| `_loop(task)` | The `for … range(MAX_LOOP_ITERATIONS)` step loop. |
| `_maybe_synthesize_wait(task, phash)` | Skip the model and return a synthetic wait when the screen is unchanged (capped at 3 in a row). |
| `_lookup_skill()` / `_lookup_ui_tree()` | Fetch the per-app hint / parse the UI tree into elements + prompt block. |
| `_loop_hint(task)` | Detect A-A, A-B-A-B, and triplet repetition in executed history. |
| `_gate_with_hitl(task, action, …)` | Run the sensitivity check, handle policy/auto-approve, pause and await the verdict. |
| `_execute_with_retry(task, action)` | Up to 4 attempts with 0.5/1.0/2.0 s backoff. |
| `_verify_outcome(task, action, pre_phash)` | Detect "nothing happened"; back-button recovery → hard fail. |
| `_save_step_artifacts(…)` | Write `step_NN.{png,xml,json,ui_prompt.txt}` for post-mortems. |

**`Task` state:** `user_id`, `description`, `state`, `step_count`, `history`, `pending_action`, `failure_reason`, `final_summary`, token counters, and `payment_pre_approved` (set when you approve a cart review so the subsequent Pay/Place-Order step auto-approves).

### 6.3 Vision / reasoning — `agent/providers/`

| File | Purpose |
|---|---|
| `base.py` | `VisionProvider` `Protocol` + `RequestUsage` / `ProviderResponse` named tuples + `ProviderError` / `QuotaExceeded`. |
| `__init__.py` | `make_provider(name, **kwargs)` factory → `gemini` \| `vertex` \| `openrouter`. |
| `gemini.py` | Google AI Studio provider (`google-genai`); free-tier defaults 5 RPM / 20 RPD. |
| `vertex.py` | Vertex AI provider (same models, GCP billing, ADC auth); 60 RPM / 10 000 RPD. |
| `openrouter.py` | OpenAI-compatible OpenRouter provider; default `google/gemini-2.5-flash`; **also implements `complete_text`** (powers the Router). |
| `throttle.py` | Async rolling-window RPM/RPD limiter shared by all providers. |
| `_parse.py` | `extract_json_object` + `salvage_truncated_json` — recover the action dict from messy/truncated output. |

**The provider contract (`base.py`):**

```python
@runtime_checkable
class VisionProvider(Protocol):
    async def get_next_action(self, screenshot_bytes, task_description, step_history,
                              screen_size=None, skill_hint=None, ui_tree=None) -> ProviderResponse: ...
    async def classify_yes_no(self, screenshot_bytes, question) -> bool: ...

class RequestUsage(NamedTuple):   input_tokens; output_tokens; rpm_remaining; rpd_remaining
class ProviderResponse(NamedTuple): action: dict; usage: RequestUsage
```

The screenshot is sent as a binary part (Gemini/Vertex) or a base64 `data:image/png;base64,…` URL (OpenRouter); the UI tree is prepended as a text block. Output is forced to JSON (`response_mime_type` / `response_format`) and parsed through the `_parse.py` salvage pipeline. **Throttle** sleeps to honor RPM and raises `QuotaExceeded` (not retryable) when the daily ceiling is hit.

### 6.4 Perception — `agent/ui_tree.py`, `agent/skills.py`, `config/prompts.py`, `skills/*.md`

| File | Purpose |
|---|---|
| `agent/ui_tree.py` | Parse `uiautomator` XML → `list[UiElement]` and render the compact prompt block. |
| `agent/skills.py` | `SkillRegistry` — lazily load + cache `skills/<package>.md` by foreground package. |
| `config/prompts.py` | The static `SYSTEM_PROMPT` (action schema + 14 rules). |
| `skills/*.md` | Per-app navigation hints / gotchas / HITL-worthy screens (one file per package). |

**`ui_tree.py` highlights:**
- Keeps a node only if it's `clickable`, has `text`/`content-desc`, or is `focused`; drops zero-area and pure-layout wrappers; suppresses an outer clickable that strictly contains a smaller one.
- Computes each element's **center** `(cx, cy)` from `bounds` — the model taps these, never raw pixels.
- Walks the XML parents to attach a **`container_label`** (the product-card title) to each `[ACTION]` button — so six identical "ADD" buttons become distinguishable.
- Tags elements with roles: **`[ACTION]`** (ADD/+/−/Checkout/Pay…), **`[CART]`** (view-cart bar), **`[LOCATION]`** (top address header), **`[CATEGORY?]`** (category tiles to avoid). Budget: 60 elements, `[ACTION]`/`[CART]` pinned first.
- Helper predicates used by the orchestrator's guards: `find_action_at`, `is_coord_in_elements`, `has_focused_text_input`, `find_smallest_element_at`.

**Action schema the model must emit (`prompts.py`):**

```jsonc
{"action": "tap",           "x": <int>, "y": <int>, "note": "<which element>"}
{"action": "type",          "text": "<str>", "note": "<str>"}
{"action": "swipe",         "x1","y1","x2","y2": <int>, "duration_ms": <int>, "note": "<str>"}
{"action": "wait",          "reason": "<str>"}
{"action": "need_approval", "reason": "<str>"}
{"action": "done",          "summary": "<str>"}
```

The 14 numbered rules enforce: stay on goal (no filters/detours), coordinates **from the element list only**, mandatory `note`, `need_approval` on payment/OTP/delete/permission, default quantity = 1, strict brand matching, go straight to cart after ADD, the 4-step typing sequence, no repeated/looping actions, and "output exactly one JSON object".

### 6.5 Device control — `device/`, `agent/action_executor.py`

| File | Purpose |
|---|---|
| `device/base.py` | `DeviceController` `Protocol` (7 async methods) + `DeviceError`. |
| `device/adb_controller.py` | Android implementation over the `adb` CLI. |
| `device/emulator.py` | `check_emulator_running(allow_physical)` — device discovery + emulator-only safety. |
| `device/ios_controller.py` | iOS implementation over Facebook's `idb` (parallel to ADB). |
| `device/ios_simulator.py` | `check_simulator_running()` — simulator-only (real iPhones always refused). |
| `agent/action_executor.py` | `execute(action, adb)` — maps an action dict to device calls. |

**`AdbController` — device actions and the adb command each runs:**

| Method | adb command |
|---|---|
| `screencap()` | `exec-out screencap -p` |
| `tap(x,y)` | `shell input tap x y` |
| `swipe(x1,y1,x2,y2,ms)` | `shell input swipe …` |
| `type_text(text)` | ADBKeyboard broadcast `ADB_INPUT_B64` (if active) else `shell input text …` |
| `key_event(code)` / `recover()` | `shell input keyevent <code>` (BACK=4, ENTER=66) |
| `launch_package(pkg)` | `shell monkey -p <pkg> -c …LAUNCHER 1` (with fuzzy fallback) |
| `get_screen_size()` | `shell wm size` (cached) |
| `get_foreground_package()` | `shell dumpsys window` → parse `mCurrentFocus` |
| `dump_ui_xml()` | the 3-pass hybrid below |
| `use_adbkeyboard_for_task()` / `restore_ime()` | `shell ime set …` |
| `reverse_tcp(port)` | `reverse tcp:<port> tcp:<port>` (exposes the host webhook to the phone over USB) |

**`dump_ui_xml()` — the UI-tree hybrid** (`_DUMP_TIMEOUT_SECONDS = 8.0` per pass):

1. **Normal dump** → best fidelity (full tree incl. `EditText`). Writes to `/sdcard/atlas_ui_dump.xml`, then `exec-out cat`s it (streaming to `/dev/tty` hangs on some OEM ROMs).
2. **Normal retry** after 250 ms — catches a screen that just settled.
3. **`--compressed` fallback** — bypasses the idle check, but can omit `EditText`/`SearchView` on some builds.

Before each pass it `rm -f`s the stale file and best-effort `killall uiautomator`s. Returns `None` only if all three passes fail. **Why it can fail:** the normal dump waits for the screen to be "idle"; continuously-animating screens (carousels, skeleton loaders, "you might also like" rows) never reach idle, so all passes time out — at which point the agent navigates from the screenshot alone (vision-only fallback).

**`action_executor.execute(action, adb)` mapping:**

| `action` | device call |
|---|---|
| `tap` | `adb.tap(x, y)` |
| `type` | `adb.type_text(text)` → 0.5 s → `key_event(66)` (ENTER, commits + dismisses keyboard) |
| `swipe` | `adb.swipe(x1,y1,x2,y2, duration_ms or 300)` |
| `wait` | `asyncio.sleep(1.0)` (no device call) |
| `done` / `need_approval` | no device call (handled by the orchestrator) |
| anything else | raises `ActionExecutionError` |

### 6.6 Security, persistence & config — `security/`, `agent/persistence.py`, `config/settings.py`

| File | Purpose |
|---|---|
| `security/hitl_gate.py` | Detect sensitive actions, pause, and block on approval. |
| `security/action_validator.py` | Normalize + allow/block actions before execution. |
| `security/audit_logger.py` | Rotating, redacted JSON-lines audit trail. |
| `agent/persistence.py` | `aiosqlite` task + step history; orphan recovery. |
| `config/settings.py` | Load `.env` → frozen `Settings`. |

**`HitlGate`** — `requires_approval(action, screenshot_b64, policy) -> bool` then `wait_for_approval(user_id, timeout) -> bool` (blocks on an `asyncio.Event`; resolved by `grant`/`deny`). Sensitivity = keyword scan of the action's narration fields (`reason/summary/text/note`) against payment / OTP / destructive / permission term sets, OR (optional, `ENABLE_VISION_HITL=1`) a vision yes/no classifier cached by perceptual hash. Any model-emitted `need_approval` always pauses. A `read_only` user attempting a state-changing action raises `ReadOnlyViolation`.

**`action_validator.validate(action) -> (ok, reason)`** — normalizes model-family aliases (`click/press`→`tap`, `input`→`type`, `scroll`→`swipe`), coerces coordinate shapes (`coordinate:[x,y]`, bbox, dict) to scalar `x`/`y`, then enforces `ALLOWED_ACTIONS = {tap, type, swipe, done, need_approval, wait}` and scans structural fields for `BLOCKED_ACTIONS = (call, sms, factory_reset, uninstall, "adb shell rm")`. A block is a hard fail (the orchestrator never executes it).

**`AuditLogger.log_action(...)`** — one JSON line per call, **twice per step** (`EXECUTING` before, `RESULT: …` after). `text/reason/summary` truncated to 50 chars; Anthropic keys and Telegram tokens regex-redacted; midnight-UTC daily rotation, 30-day retention at `<LOG_DIR>/audit.log`.

**`TaskRepository`** (`aiosqlite`) — `tasks` + `steps` tables (schema in §11 notes). `insert_task`/`update_state`/`append_step` during the run; **`recover_orphans()` at startup** flips any task left non-terminal (DONE/FAILED/TIMED_OUT) to FAILED ("bot restarted while running") — this is what cleans up a task interrupted by a restart. `list_recent(user_id)` powers `/history`.

---

## 7. The agent loop internals

### 7.1 The guard stack (structural rejections)

After `validate()` and before the HITL gate, the action runs a gauntlet of nine checks. A fired guard appends a corrective hint to `task.history`, audit-logs a tag, forces a real model call next step, and `continue`s — it never reaches the device.

| Guard | Catches | Audit tag |
|---|---|---|
| Stale-tree | tap/swipe while the tree (previously seen) is now empty; rejects up to 2× then **overrides** to vision-only | `STALE_TREE_REJECTED` / `_OVERRIDE` |
| Coordinate grounding | tap/swipe whose start point lands on no element (20 px margin) | `COORD_REJECTED` |
| Intent mismatch | "search bar" note landing on the `[LOCATION]` header; ADD/checkout note landing on a non-`[ACTION]` element | `INTENT_MISMATCH_REJECTED` |
| Product-name mismatch | ADD tap whose claimed product doesn't match the `container_label` under those coords | `PRODUCT_NAME_MISMATCH_REJECTED` |
| Category tap | tapping a category/tile/banner when the task didn't ask to browse | `CATEGORY_TAP_REJECTED` |
| Repeated ADD | a second "fresh ADD" at the same ±10 px coords as the last tap | `REPEATED_ADD_REJECTED` |
| Type without focus | a `type` action with no `focused=true` text input in the tree | `TYPE_WITHOUT_FOCUS_REJECTED` |
| Premature cart review | `need_approval` "Cart review" while on a search screen (no cart markers) | `premature cart-review` |
| Giveup | "no products found"-style `need_approval` before any real swipe, or contradicting a just-executed ADD | `giveup before scrolling` |

### 7.2 Dedup vs. loop detection (perceptual hash)

- **Dedup** (`_maybe_synthesize_wait`): if the current screen's phash **equals** the last one the model saw, inject a `wait` and skip the model — but at most 3 in a row.
- **Loop detection** (`_loop_hint`): over *executed* history, detect **A-A**, **A-B-A-B**, or a **triplet** (same action ≥3×). Each firing adds a warning to the next prompt and increments `_loops_detected`; at **`MAX_LOOPS_BEFORE_ABORT = 4`** the task hard-fails.
- **Outcome verification** (`_verify_outcome`): after a state-changing action, re-hash; an unchanged screen escalates — back-button recovery at streak 2, `OrchestratorError` at streak 3.

### 7.3 Session-level guards

| Guard | Value | Enforcement | On breach |
|---|---|---|---|
| Session timeout | `SESSION_TIMEOUT_SECONDS` (default 180; **deployed 600**) | `asyncio.wait_for(_loop, timeout)` | → `TIMED_OUT` |
| Max iterations | `MAX_LOOP_ITERATIONS = 30` | `for` counter | → `FAILED` ("exceeded 30 iterations") |
| Max loops | `MAX_LOOPS_BEFORE_ABORT = 4` | checked each step | → `FAILED` ("stuck in a loop") |

### 7.4 Task states

```
IDLE ──run_task──▶ RUNNING ──"done"──▶ DONE                     (success, terminal)
                     │  └─sensitive──▶ AWAITING_APPROVAL ──grant──▶ RUNNING
                     │                                    └─deny──▶ FAILED ("user denied approval")
                     ├── any OrchestratorError / validator block / retries exhausted ──▶ FAILED
                     └── asyncio.TimeoutError ─────────────────────────────────────────▶ TIMED_OUT
```
`DONE`, `FAILED`, `TIMED_OUT` are terminal and always persisted.

---

## 8. Safety model

1. **Emulator-only by default.** Startup refuses to run if a physical device is attached unless `ALLOW_PHYSICAL_DEVICE=1` (and warns loudly when it is).
2. **Authorization.** Only users in `users.json` may trigger; onboarding is via admin-issued `/pair` codes (5-min TTL, single-use, constant-time compare). Per-user policy: `always_approve` / `confirm_sensitive` (default) / `read_only`.
3. **Action validator.** Explicit allow-set; blocklist for calls, SMS, factory reset, uninstall, `adb shell rm`. Hard fail before execution.
4. **HITL gate** on payment, OTP, delete/uninstall, and permission screens (keyword + optional vision classifier), plus any model-emitted `need_approval`. The loop blocks until you tap Approve/Deny in Telegram.
5. **Audit before action.** Every action is logged `EXECUTING` *before* it runs and `RESULT` after, with secret redaction and daily rotation.
6. **Webhook security.** Timing-safe shared-secret header; bound to `127.0.0.1` and reached only over the USB `adb reverse` tunnel; empty secret disables the server entirely.
7. **Secrets never on disk/logs.** Config comes from `.env`; keys/tokens are redacted from the audit trail.

---

## 9. Configuration reference

All config is read from `.env` (search order: explicit path → `~/.mobile-agent/.env` → `./.env`) into a frozen `Settings`. A template is written to `~/.mobile-agent/.env.example` on first run.

| `.env` key | Default | Required when | Purpose |
|---|---|---|---|
| `VISION_PROVIDER` | `gemini` | always | `gemini` \| `vertex` \| `openrouter` (**deployed: `openrouter`**) |
| `GEMINI_API_KEY` | — | provider=gemini | Google AI Studio key |
| `GEMINI_MODEL` | `gemini-2.0-flash` | — | model for gemini/vertex |
| `GCP_PROJECT_ID` | — | provider=vertex | Vertex project |
| `GCP_LOCATION` | `us-central1` | — | Vertex region |
| `GOOGLE_APPLICATION_CREDENTIALS` | — | provider=vertex | service-account JSON path (must exist) |
| `OPENROUTER_API_KEY` | — | provider=openrouter | OpenRouter key |
| `OPENROUTER_MODEL` | `google/gemini-2.5-flash` | — | OpenRouter model slug |
| `TELEGRAM_BOT_TOKEN` | — | **always** | bot token |
| `TELEGRAM_ADMIN_ID` | — | for pairing | admin user; default webhook owner |
| `TELEGRAM_ALLOWED_USER_IDS` | — | — | deprecated; migrated once into `users.json` |
| `ANDROID_DEVICE_ID` | `emulator-5554` | **always** | adb `-s` target serial |
| `ALLOW_PHYSICAL_DEVICE` | off | — | `1` to drive a real phone (**deployed: `1`**) |
| `ENABLE_VISION_HITL` | off | — | `1` adds a vision sensitivity classifier (doubles model calls) |
| `WEBHOOK_SECRET` | — | — | enables the trigger server; empty = disabled |
| `WEBHOOK_OWNER_USER_ID` | = `TELEGRAM_ADMIN_ID` | — | user a webhook trigger acts as |
| `WEBHOOK_HOST` / `WEBHOOK_PORT` | `127.0.0.1` / `8765` | — | trigger server bind + `adb reverse` port |
| `SESSION_TIMEOUT_SECONDS` | `180` | — | whole-task timeout (**deployed: `600`**) |
| `LOG_DIR` | `~/.mobile-agent/logs` | — | audit log directory |
| `MOBILE_AGENT_CONFIG_DIR` | `~/.mobile-agent` | — | base dir for `.env`, `users.json`, `tasks.db` |

`MAX_LOOP_ITERATIONS` (30) and `MAX_LOOPS_BEFORE_ABORT` (4) are constants in `orchestrator.py`, not env vars.

---

## 10. Setup & run

**Prerequisites:** Python 3.14, `adb` on `PATH`, a running Android emulator (or a USB phone with USB debugging), and a Telegram bot token.

```bash
# 1. Install
python -m venv .venv
.venv/bin/pip install -r requirements.txt

# 2. Configure
cp .env.example ~/.mobile-agent/.env     # then edit: provider key, TELEGRAM_BOT_TOKEN, ANDROID_DEVICE_ID
adb devices                               # confirm your device serial

# 3. Run
.venv/bin/python -u main.py
```

> **Always launch with the venv interpreter** (`.venv/bin/python`). Running with a bare system/Homebrew Python fails with `ModuleNotFoundError` because the dependencies live in the venv.

**Health check & restart (when driving a USB phone on port 8765):**
```bash
curl -s http://127.0.0.1:8765/health        # {"ok": true}
adb reverse --list                           # UsbFfs tcp:8765 tcp:8765
# restart: kill the old PID, wait for the port to free, relaunch with .venv/bin/python, then re-run adb reverse
```

**Trigger by voice (HTTP shortcut):**
```bash
curl -X POST http://127.0.0.1:8765/trigger \
  -H "X-Webhook-Secret: $WEBHOOK_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"text": "order milk on blinkit", "autorun": "true"}'
```

**Smoke test without Telegram:** `.venv/bin/python test_local.py`. **Tests:** `.venv/bin/pytest`.

---

## 11. Project layout

```
mobile-agent/
├── main.py                     # entry point + dependency wiring
├── requirements.txt
├── .env.example
├── bot/
│   ├── telegram_bot.py         # PTB Application factory + handler registration
│   ├── handlers.py             # all Telegram callbacks + webhook→orchestrator bridge
│   ├── webhook.py              # aiohttp POST /trigger + GET /health
│   ├── router.py               # LLM intent classifier → Route
│   ├── apps.py                 # 23-app registry + task templates
│   ├── session.py              # per-user menu state
│   ├── users.py                # authorization store + policy
│   └── pairing.py              # /pair onboarding codes
├── agent/
│   ├── orchestrator.py         # THE agent loop
│   ├── state_machine.py        # TaskState + Task
│   ├── phash.py                # perceptual hash
│   ├── computer_use.py         # provider shim
│   ├── ui_tree.py              # uiautomator XML → compact element listing
│   ├── skills.py               # per-app skill loader
│   ├── action_executor.py      # action dict → device calls
│   ├── persistence.py          # aiosqlite task/step history
│   └── providers/              # base, __init__ (factory), gemini, vertex, openrouter, throttle, _parse
├── security/
│   ├── hitl_gate.py            # human-in-the-loop approval
│   ├── action_validator.py     # allow/block + normalization
│   └── audit_logger.py         # redacted JSON audit trail
├── device/
│   ├── base.py                 # DeviceController Protocol
│   ├── adb_controller.py       # Android (adb)
│   ├── emulator.py             # device discovery + emulator-only guard
│   ├── ios_controller.py       # iOS (idb)
│   └── ios_simulator.py        # simulator-only guard
├── config/
│   ├── settings.py             # .env → Settings
│   └── prompts.py              # SYSTEM_PROMPT (schema + 14 rules)
└── skills/                     # com.<package>.md per-app hints (+ README.md authoring guide)
```

**Runtime artifacts** (under `~/.mobile-agent/`): `.env`, `users.json`, `tasks.db`, `logs/audit.log`, and per-task screenshot/step dumps. The `tasks` table stores `id, user_id, description, state, started_at, ended_at, final_summary, failure_reason, step_count, total_input_tokens, total_output_tokens`; the `steps` table stores `task_id, idx, action_json, result, timestamp`.

---

## 12. Known limitations & design notes

- **Animated screens defeat the UI dump.** `uiautomator dump` waits for an idle screen; continuously-animating UIs (carousels, "you might also like", skeleton loaders) never idle, so all three dump passes time out (~24 s total) and the agent falls back to **vision-only** navigation. It still works, but it's slower and less precise on those screens. A persistent UiAutomator2 server is the robust long-term fix.
- **OEM ROM constraints.** On locked-down ROMs (e.g. ColorOS) the shell can't disable animations, kill processes, or change secure settings — hence the file-based dump and the ADBKeyboard IME workaround for reliable typing.
- **Router needs `complete_text`.** Intent routing is active only with a provider that implements it (OpenRouter today). Other providers fall back to the freeform agent path.
- **Free-tier throttling is real.** With the Gemini provider's 5 RPM / 20 RPD defaults, a single multi-step task can exhaust the daily quota; use a paid tier or OpenRouter for sustained use.
- **iOS is implemented but lighter.** `IdbController` covers the full `DeviceController` protocol, but there's no accessibility-tree dump, so iOS runs vision-only.
- **Single active device.** One bound device per process (`ANDROID_DEVICE_ID`).
```
