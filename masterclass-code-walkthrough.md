# Masterclass — Code Walkthrough Edition
## "Atlas: Inside a Production Mobile Computer-Use Agent"

**Format:** 60 minutes, live, code-on-screen + a running agent beside it
**Audience:** SDEs and technical generalists evaluating the course
**Prerequisite read:** pair this with `masterclass-blueprint.md` (the demo/wrapping edition). This doc is for when your manager green-lights *showing the code*.

> ⚠️ Confirm with your manager before presenting source. If approved, this is the script. If not, fall back to `masterclass-blueprint.md` and present the architecture diagrams only.

---

## 0. The structural idea: *follow one sentence through the code*

Do **not** tour the repo file-by-file (10k LOC, 21 source files — death by directory tree). Instead, take **one instruction — "order milk on blinkit" — and follow it through every layer of the system.** Each layer is a code stop. The audience watches a single photon travel: Telegram → router → orchestrator loop → vision → guards → device → back. By the end they've seen the whole codebase, but as a *story*, not a catalogue.

**The throughline (same as the demo edition, sharpened for engineers):**
> **"The model is wrong constantly. This codebase is the engineering that makes it right anyway."**

Every stop reinforces it: the grounding that stops hallucinated taps, the 9 guards, the HITL gate, the salvage parser, the orphan recovery. The AI is ~250 lines of provider glue. The other 9,800 lines are the craft. *That craft is the course.*

---

## 1. The codebase at a glance (one slide, ~30s)

10,114 lines across 5 clusters. Keep this map visible as a "you are here" anchor all hour.

| Cluster | Lines | Role in the journey |
|---|---|---|
| `bot/` (front door) | ~1,900 | Telegram + voice intake, auth, LLM intent router |
| `agent/` (brain + loop) | ~3,900 | The orchestrator loop, UI-tree grounding, providers |
| `device/` (hands) | ~700 | ADB/idb control behind one Protocol |
| `security/` (conscience) | ~450 | HITL gate, action validator, audit log |
| `config/` (rules) | ~430 | Frozen settings, the system prompt |
| **Tests** | ~2,500 | 30+ test files — the "this is production" proof |

**Headline architecture facts** (say these out loud — they pre-empt the skeptics):
- **No agent framework.** No LangGraph, no LangChain. A hand-written async state machine (`agent/orchestrator.py`, 2,454 lines).
- **No per-app integration.** No Blinkit API, no scraping. Vision + the accessibility tree.
- **Pluggable brain.** The vision model is a `Protocol` — swap Gemini ↔ Claude ↔ GPT-4V via one env var.
- **Cross-platform hands.** Android (ADB) and iOS (idb) implement the same 7-method `DeviceController` Protocol.

---

## 2. Minute-by-minute (the 12 code stops)

> Timings include a running agent on a second screen. Aim to *show code, then make it fire live* at each major stop — the hybrid is what lands. Q&A folded into the last 3 min; hard questions are a gift (they re-sell the engineering).

---

### STOP 0 — Cold open (0:00–0:03)

Same as the demo edition: trigger `order milk on blinkit` live, let it run autonomously and **pause at checkout.** One sentence: *"That just traveled through 10,000 lines of code. For the next hour, we follow it — and you'll see the AI is the easy part."*

Leave the run going on the side screen while you start the code tour.

---

### STOP 1 — The front door: intake, auth, routing (0:03–0:09)

**On screen:** `bot/handlers.py`, `bot/router.py`, `bot/apps.py`

**1a. Auth first, always** — `bot/handlers.py:115` `_is_paired()` / `_is_admin()`. Every entry point checks `UserStore.is_allowed()` before anything. Onboarding is admin-issued `/pair` codes: **5-min TTL, single-use, constant-time compare** (`bot/pairing.py`). 
> Teach: *"The first line of every handler is authorization. Not the fifth. The first."*

**1b. The LLM intent router** — `bot/router.py:route()`. This is the wow for engineers. There is **no hard-coded alias map.** "blinkit", "cab", "order me milk" are resolved by an LLM classifier over a *fixed catalogue* of 23 apps × 7 categories (`bot/apps.py`). The model must answer with only:
```json
{"app_id": "blinkit", "task_id": "order", "param": "milk"}
```
And critically — `_parse_response` **validates those ids against the live registry**. A hallucinated `app_id` resolves to `None` and *safely falls back to freeform*. 
> Teach: *"The LLM picks; the code verifies against a real registry. Hallucinate an app that doesn't exist? You get None, not a crash. That pattern — LLM proposes, code disposes — repeats in every layer."*

**1c. The confirm-first voice flow** — `bot/handlers.py:402` `handle_external_trigger` → `handle_external_confirm`. Two phases: phase 1 strips the wake word (`_strip_wake_word`, line 52), routes the intent, stashes it with a **90-second TTL**, and *speaks back* "Got it — add milk on Blinkit. Confirm?"; phase 2 fires only on confirmation. `autorun=true` collapses both for trusted shortcuts.
> Wow: this is the "where native can't go" beat in code — hands-free, with a spoken safety confirm.

---

### STOP 2 — The wiring: how it all connects (0:09–0:13)

**On screen:** `main.py:70` `main()`

This is the dependency-injection spine. Walk the construction order — it reads like an assembly manifest:
```
check_emulator_running(allow_physical)   # main.py:47 — refuse a real phone unless opted in
AdbController(device_id)                  # :88
HitlGate()                                # :96
AuditLogger(log_dir)                      # :97
make_provider(vision_provider, …)         # :98 — the swappable brain
SkillRegistry(skills/)                    # :129
TaskRepository(db_path) → recover_orphans()  # :131-133 — crash recovery on boot
Orchestrator(adb, hitl, audit, vision, …) # :140
Router(vision)  ONLY if provider has complete_text  # :162
Handlers(…) + register_handlers           # :165
orchestrator.on_status_update / on_approval_request = handlers.…  # :176-177
_wire_webhook(…)                          # :181 — voice server, only if WEBHOOK_SECRET set
app.run_polling()                         # :186
```
Two teaching points:
- **The Protocol seam (`main.py:98`).** `make_provider` returns *something that satisfies `VisionProvider`* — the orchestrator never imports a concrete provider class. Same for `AdbController` vs the `DeviceController` Protocol. *"You can swap the brain or the hands without touching the loop."*
- **The Router is conditional (`main.py:162`).** It only wires up if the provider implements `complete_text` — currently only OpenRouter. With Gemini, every message goes freeform. *"Capabilities are detected, not assumed."*

---

### STOP 3 — The brain seam: pluggable vision (0:13–0:18)

**On screen:** `agent/providers/base.py`, then `gemini.py` vs `openrouter.py` side by side

**3a. The contract** — `base.py:27` `@runtime_checkable class VisionProvider(Protocol)`. Two methods: `get_next_action(...)` and `classify_yes_no(...)`. Returns a `ProviderResponse` NamedTuple = `(action: dict, usage: RequestUsage)`.
> Teach: *"Why a Protocol, not a base class? Because a provider doesn't have to inherit anything — structural typing. And `@runtime_checkable` lets `main.py` ask `isinstance(p, VisionProvider)` at runtime. This is modern Python contract design."*

**3b. The same idea, two encodings** — show the one real difference between providers:
- Gemini (`gemini.py:85`): screenshot sent as a **binary `Part.from_bytes`**.
- OpenRouter (`openrouter.py:163`): screenshot **base64-encoded into a `data:image/png;base64,…` URL**.
> Wow: *"Same abstraction, different wire format — and the orchestrator knows about neither. THIS is what a Protocol buys you. Change `VISION_PROVIDER=openrouter` to `gemini` and the entire loop is unchanged."*

**3c. The hero function — `salvage_truncated_json`** (`agent/providers/_parse.py:39`). When the model runs out of output budget mid-JSON — `{"action":"tap","x":265,"y":1287,"note":"...` — this walks the string, finds the last *safe* truncation boundary, and closes the brackets in reverse-stack order to recover `{"action":"tap","x":265,"y":1287}`. It loses the unfinished field, not the whole step.
> Wow: *"A truncated model response would normally fail the entire task. This reconstructs a valid action from the wreckage. That's the difference between a demo and a product."* This is one of the single most impressive ~100 lines in the repo — linger here.

**3d. Three-layer parse** (`gemini.py:178`): `json.loads()` → `extract_json_object()` (strips prose/fences) → `salvage_truncated_json()`. Trust the forced-JSON output (`response_mime_type="application/json"`), but verify with three fallbacks anyway.

---

### STOP 4 — Perception: turning a screenshot into grounded choices (0:18–0:24)

**On screen:** `agent/ui_tree.py`, `device/adb_controller.py`

This is the most counterintuitive part for engineers and the strongest "oh, *that's* how" moment.

**4a. The problem:** a raw screenshot makes the model invent coordinates. **The fix:** feed it the accessibility tree as a compact element list with *real* center coordinates.

**4b. `ui_tree.parse()`** (`ui_tree.py:166`) — keeps a node only if it's `clickable` / has text / is focused; drops zero-area layout wrappers; **suppresses an outer clickable that contains a smaller one** (`_suppress_wrapping_clickables`, :337). Computes each element's **center** from bounds — *the model taps these, never raw pixels.*

**4c. The killer detail — container labels** (`_find_container_label`, :297). It walks XML parents to attach the **product-card title to each ADD button.**
> Wow: *"Six identical 'ADD' buttons on a search page become six distinguishable choices — `[ACTION] ADD for \"Amul Milk 500ml\"`. The model isn't guessing which ADD; the tree told it."* Show a real rendered `ui_prompt.txt` from a step artifact if you have one.

**4d. Role tagging** (`_is_action_like` :56, `_is_location_header` :82, `_is_category_like` :106): `[ACTION]`, `[CART]`, `[CATEGORY?]`, `[LOCATION]`. Budget of 60 elements, `[ACTION]`/`[CART]` pinned first.

**4e. The 3-pass UI dump** — `adb_controller.py:328` `dump_ui_xml()`. This is a "you only learn this by shipping on real phones" gem:
1. Normal `uiautomator dump` → best fidelity, *but requires the screen to be idle.*
2. Retry after 250ms — catches a screen that just settled.
3. `--compressed` fallback — bypasses the idle check, but can omit `EditText` on some builds.
> Teach: *"Continuously-animating screens (carousels, skeleton loaders) never reach idle, so the dump times out — and then the agent navigates from the screenshot alone. Every pass is a real production scar."* Bonus detail: each pass is **fused into one `adb exec-out sh -c` round-trip** (`_try_dump`, :372) — 9× fewer USB round-trips — and the `rm -f` before each dump is load-bearing (a failed dump writes nothing; stale file = reading last screen as current).

**4f. The ADBKeyboard swap** (`type_text` :132, `use_adbkeyboard_for_task` :157). `adb shell input text` loses a focus race; the agent swaps the IME to ADBKeyboard at task start and sends text via a base64 broadcast straight to `commitText()`, restoring the user's keyboard in `finally`. *"You don't fix the OEM's bug — you route around it."*

---

### STOP 5 — The rules: the system prompt (0:24–0:28)

**On screen:** `config/prompts.py`

**5a. The action schema** (`prompts.py:26`) — six actions, coordinates as **plain scalar integers**, with explicit WRONG examples (`coordinate:[x,y]`, `bbox`, `click`). *"We show the model exactly how it tends to fail, inline."*

**5b. The 10 rules** (`:44`) — top rules dominate. The standout for engineers is **Rule 2 and Rule 5**, which literally tell the model that *the orchestrator will structurally reject* a mismatched tap or a type-without-focus:
> *"the orchestrator cross-checks the `note` field of every tap against the element actually under the coords… the tap is REJECTED."*

> Wow: *"The prompt and the code enforce the same contract from both sides. The prompt asks nicely; the orchestrator enforces it in Python. Belt and suspenders."*

**5c. The commerce addendum** (`:126`) — note it's a *separate* string. This is the segue to the generalization stop: shopping rules (forbidden taps, brand matching, cart-review HITL) are **only injected for commerce apps**, never for WhatsApp or Maps.

---

### STOP 6 — Generalization: how one loop drives any app (0:28–0:33)

**On screen:** `agent/profiles.py`

This is the keystone of "win where native can't go," expressed in ~240 elegant lines.

**6a. Two profiles** — `GENERIC` (`profiles.py:133`) is `AppProfile(name="generic")`: empty vocabulary, all shopping guards off. `COMMERCE` (`:162`) carries the `[ACTION]`/`[CART]`/`[CATEGORY?]`/`[LOCATION]` token sets and `enforce_shopping_guards=True`, shared by 9 commerce packages (`:142`).

**6b. The resolution** — `resolve_profile(package)` (`:231`): **unknown/None package → GENERIC.** A never-before-seen app runs the lean, structural-only path.
> Wow: *"This is why it works on an app I've never configured. No profile? It falls back to GENERIC — coordinate grounding, type-focus, loop detection still run (those are universal), but it skips the grocery heuristics that would only mis-fire. Generality isn't coded per app; it's the* default *."*

**6c. The `annotates` fast-path** (`:106`) — GENERIC skips all annotation work in the parser/renderer. *"Non-commerce apps pay nothing for shopping grounding — correctness AND efficiency."*

> Tie to the docstring at `:9` — it literally narrates the refactor from hardcoded grocery globals to per-app profiles. Show it; it's the design rationale in the author's own words.

---

### STOP 7 — THE LOOP (0:33–0:43) — *the centerpiece, 10 minutes*

**On screen:** `agent/orchestrator.py` — `run_task` (`:421`) and `_loop` (`:514`)

This is the heart. Budget the most time here. Walk the pipeline top to bottom; it's one `for` loop over `MAX_LOOP_ITERATIONS = 30`.

**7a. `run_task` setup** (`:421`) — reset ~15 per-task counters, wake the screen (*"a sleeping display returns all-black screenshots; one live run wasted its whole session that way"* — `:447`), swap the IME, optionally launch the app, then the whole loop is wrapped in `asyncio.wait_for(timeout)`. The `except` ladder maps cleanly to terminal states: `TimeoutError → TIMED_OUT`, `OrchestratorError → FAILED`, and `finally:` always restores the IME.

**7b. The pipeline** — narrate each step as one slide-with-a-pointer. The order *is* the lesson:
```
screencap → perceptual hash                          (:522)
  → dedup: unchanged screen? synthesize a wait, skip the model   (:538, _maybe_synthesize_wait :1014)
  → resolve foreground pkg → profile → skill hint     (:553)
  → dump UI tree → parse → compact listing            (:556)
  → loop detection (A-A, A-B-A-B, triplet)            (:583, _loop_hint :1757)
  → vision.get_next_action(screenshot, tree, history, hints)  (:610)
  → validate(action)  — hard fail on block            (:659)
  →  ┌─ THE GUARD GAUNTLET (9 structural rejections) ─┐
  → gate_with_hitl — pause on sensitive screens        (:970)
  → audit "EXECUTING"  (BEFORE acting, never after)    (:973)
  → execute_with_retry                                 (:988)
  → audit "RESULT" → persist step                      (:990-993)
  → verify_outcome — did the screen actually change?   (:1006)
```

**7c. The guard gauntlet** (`:666`–`:967`) — *this is the "model proposes, harness disposes" payload.* Each guard appends a corrective hint to history, audit-logs a tag, and `continue`s — **it never reaches the device.** Walk 3–4 of the nine; don't read all nine:
| Guard | Catches | Line |
|---|---|---|
| Stale-tree | tap/swipe when the dump returned nothing (reject ≤2×, then override to vision-only) | :673 |
| Coord grounding | tap whose start lands on *no element* (20px margin) | :723 |
| Intent mismatch | "search bar" note landing on the `[LOCATION]` header | :746 |
| Product-name mismatch | ADD tap whose product ≠ the `container_label` under the coords ("claimed eggs, added a Fire TV Stick" — :770) | :773 |
| Category tap | tapping a banner/tile when the task didn't ask to browse | :796 |
| Repeated-ADD | a 2nd ADD at the same ±10px as the last (the pixels are now a "−1+" stepper) | :818 |
| Excess-quantity | requested units already in cart (the live "3 milks instead of 1" bug) | :841 |
| Type-without-focus | `type` with no focused EditText in the tree | :865 |
| Premature cart-review / giveup / benign-approval | `need_approval` abuse | :889, :916, :949 |

> Wow (live): point at the **product-name mismatch** guard, then to the running agent and say *"the comment on line 770 is a real incident — the model claimed eggs and tried to add a Fire TV Stick. This guard caught it. The model will be wrong on stage too; watch the harness catch it."* If you can force a rejection live (search a brand it'll fumble), the `step N: coords rejected, retrying` status message landing in Telegram is pure gold.

**7d. The self-correction loop** — `_verify_outcome` (`:1984`): after a state-changing action, re-hash; an unchanged screen escalates — back-button recovery at streak 2, hard fail at streak 3. Plus `_maybe_synthesize_wait` (dedup, capped at 3) and `_loop_hint` (A-A / A-B-A-B / triplet, hard-fail at 4 loops). *"The agent notices when it's stuck and breaks itself out — or gives up cleanly instead of burning your quota."*

---

### STOP 8 — The conscience: safety in code (0:43–0:49)

**On screen:** `security/hitl_gate.py`, `security/action_validator.py`, `security/audit_logger.py`

Pay off the checkout pause from the cold open — *in the code that caused it.*

**8a. The gate** — `hitl_gate.py:64` `requires_approval()`: keyword-scans the action's narration fields against payment/OTP/destructive/permission terms; **OR** an optional vision classifier (`classify_sensitivity` :137) that's **cached by perceptual hash** so a static screen never burns a second vision call (LRU, 256). `wait_for_approval` (:103) blocks on an `asyncio.Event` — no polling — resolved by `grant`/`deny` from the Telegram callback.
> Wow (live): tap **Deny** in Telegram → watch the task stop cold. Then show the **payment latch**: `orchestrator.py:1950` sets `task.payment_pre_approved = True` when you approve a cart review, so the later "Place Order" tap doesn't ask twice — *"approve the cart once, authorize the checkout; but OTP still pauses separately."*

**8b. The validator** — `action_validator.py:145` `validate()`: an explicit **allow-set** (`tap/type/swipe/done/need_approval/wait`) and a **blocklist** (`call`, `sms`, `factory_reset`, `uninstall`, `adb shell rm`) — a hard fail before execution. The clever bit: it scans only *structural* fields, exempting `_CONTENT_FIELDS = {text, reason, summary, note}` (:34) so a legit `"text": "call mom"` isn't false-blocked. And `_normalize_coords` (:99) absorbs every model-family coordinate dialect (Qwen's `coordinate:[[x,y]]`, bboxes, dicts) into scalar `x/y` so downstream code sees one shape.
> Teach: *"Security that doesn't break legitimate input. The rule isn't 'block the word delete' — it's 'block structural commands, allow user content.'"*

**8c. The audit log** — `audit_logger.py:65` `log_action()`: one JSON line **twice per step** (EXECUTING before, RESULT after — security rule 4), with regex redaction of API keys/tokens (`:12`), 50-char truncation of content fields, and **UTC-midnight daily rotation, 30-day retention.** *"Even if a key leaks into a `note`, it never hits disk."*

---

### STOP 9 — Resilience: surviving the real world (0:49–0:53)

**On screen:** `agent/persistence.py`, `agent/providers/throttle.py`, `agent/state_machine.py`

**9a. Orphan recovery** — `persistence.py:189` `recover_orphans()`. Called at boot (`main.py:133`): any task left non-terminal by a crash/Ctrl-C is flipped to `FAILED("bot restarted while running")`.
> Wow (live, if brave): kill the bot mid-task, restart, show the orphaned task reconciled and the user notified. *"You can't prevent crashes; you can refuse to lie about state afterward."*

**9b. The throttle** — `throttle.py:26` `acquire()`: a **rolling-window** limiter (not a fixed bucket — no "hit the wall at 23:59, wait a minute"). **RPM is a soft gate (sleep), RPD is fatal (`QuotaExceeded`, non-retryable).** *"Two rate limits, two different consequences, encoded as two different control-flow paths."*

**9c. The state machine** — `state_machine.py:9` `TaskState`: `IDLE → RUNNING → {DONE | FAILED | TIMED_OUT | AWAITING_APPROVAL}`. Plus three session guards: 600s timeout, 30 iterations, 4 loops. *"A plain enum and a dataclass. No framework. You can hold the entire state model in your head."*

---

### STOP 10 — The "this is real" proof: tests + no framework (0:53–0:56)

**On screen:** the test files in the tree (`pytest` run if you've pre-warmed it)

- **30+ test files, ~2,500 lines** — `test_action_validator.py` (209), `test_webhook.py` (380), `test_handlers.py` (263), `test_router.py`, `test_orchestrator.py`, and a `fixtures/` dir with a real captured `blinkit_eggs_search_results.xml`.
> Teach: *"The guards aren't vibes — every rejection has a test pinning its behavior against a real captured UI tree."*
- **No agent framework**, restated with the code as evidence: a 2,454-line hand-written async loop, `asyncio.Event` for HITL, `asyncio.wait_for` for timeouts. *"You don't need a framework. You need to understand the loop. That understanding is transferable to any agent you build."*

---

### STOP 11 — Close + course pitch + Q&A (0:56–1:00)

Bridge the curriculum directly onto the code they just read:

| The code that impressed them | What the course teaches |
|---|---|
| `_loop` + the 9 guards | Designing self-correcting agent loops |
| `ui_tree.py` container labels | Grounding: turning pixels into verifiable choices |
| `VisionProvider` Protocol | Provider-agnostic architecture & seams |
| `hitl_gate` + validator + audit | Safety, authz, and auditability for autonomous systems |
| `salvage_truncated_json` | Defensive parsing & graceful degradation |
| `profiles.py` GENERIC fallback | Generalization without per-target code |

Close on the throughline: *"The model was the easy part. Everything you found interesting today was engineering — and engineering is learnable. That's the course."* Then the CTA slide, then Q&A.

**Seed Q&A** (engineers will ask these — have crisp answers):
- *"How often is the model wrong?"* → "Constantly. That's why there are nine guards and outcome verification. The harness assumes the model is unreliable."
- *"Why no LangGraph?"* → "We wanted to own the control flow — guards, HITL, recovery are all custom. A framework would've been in the way." (`orchestrator.py` is the evidence.)
- *"Isn't vision slow/expensive?"* → "Yes — hence perceptual-hash dedup, the synthetic-wait skip, loop detection, and throttling. Cost control is a first-class concern, not an afterthought."
- *"Does it work on apps you didn't configure?"* → "Yes — `resolve_profile` returns GENERIC for unknowns. Show `profiles.py:231`."

---

## 3. Presenter cue card — jump-to-line bookmarks

Pre-bookmark these in your editor (VS Code: `Ctrl+G <line>`). Do **not** scroll-hunt live — it kills momentum.

| # | What | File:line |
|---|---|---|
| 1 | Auth-first check | `bot/handlers.py:115` |
| 2 | LLM intent router | `bot/router.py` `route()` |
| 3 | Voice confirm-first | `bot/handlers.py:402` |
| 4 | Wiring / DI | `main.py:70` |
| 5 | Provider Protocol | `agent/providers/base.py:27` |
| 6 | Binary vs base64 image | `gemini.py:85` / `openrouter.py:163` |
| 7 | **Truncation salvage** | `agent/providers/_parse.py:39` |
| 8 | UI-tree parse | `agent/ui_tree.py:166` |
| 9 | **Container labels** | `agent/ui_tree.py:297` |
| 10 | 3-pass UI dump | `device/adb_controller.py:328` |
| 11 | System prompt rules | `config/prompts.py:44` |
| 12 | **GENERIC fallback** | `agent/profiles.py:231` |
| 13 | `run_task` | `agent/orchestrator.py:421` |
| 14 | **`_loop` (centerpiece)** | `agent/orchestrator.py:514` |
| 15 | Guard gauntlet start | `agent/orchestrator.py:666` |
| 16 | Product-name guard ("Fire TV" incident) | `agent/orchestrator.py:770` |
| 17 | HITL gate | `security/hitl_gate.py:64` |
| 18 | Payment latch | `agent/orchestrator.py:1950` |
| 19 | Action validator | `security/action_validator.py:145` |
| 20 | Orphan recovery | `agent/persistence.py:189` |

---

## 4. Live code-reading craft (how not to bore or lose them)

1. **Narrate the data, not the syntax.** "The screenshot becomes bytes, then a hash, then — if the screen changed — a model call." Never read code aloud line by line.
2. **One pointer, one idea.** Highlight the 3–5 lines that matter; gray out the rest. The guard gauntlet is 300 lines — show the *shape* (reject → hint → continue), then 3 examples.
3. **Code, then fire.** After explaining a guard, switch to the running agent and make it fire. The code→behavior loop is the whole point.
4. **Comments are your script.** This codebase has unusually narrative comments (the "Fire TV Stick" incident at `:770`, the "device dozed off" note at `:447`, the `profiles.py` rationale at `:9`). Read *those* aloud — they're war stories, and war stories sell.
5. **Resist the tour.** You will be tempted to show one more clever function. Don't. Twelve stops, on time, beats twenty stops that overrun.

---

## 5. Pre-flight (code-edition additions)

In addition to the demo-edition checklist in `masterclass-blueprint.md`:
- [ ] Editor at a back-row-legible font; theme high-contrast; minimap off; line numbers on.
- [ ] All 20 cue-card locations bookmarked / in open tabs.
- [ ] A captured `step_NN.ui_prompt.txt` artifact open (to show a *real* rendered element listing with `[ACTION] for "…"` labels).
- [ ] `pytest` already run once in a terminal tab (green output ready to show; don't run live — a flake mid-talk is a disaster).
- [ ] `git log --oneline` clean and presentable if you'll show commit history (the `optimization` branch messages tell a nice story).
- [ ] Side-by-side layout rehearsed: code editor + running agent + Telegram all visible or quick-switchable.
- [ ] Decide your one *forced-rejection* demo and test it today (e.g., a brand the model fumbles → watch a guard fire live). Have the recording as backup.

---

## 6. The 30-minute cut (if your slot shrinks)

Keep STOPS 0, 4 (perception), 7 (the loop), 8 (safety), 11 (close). Drop 1–3, 5–6, 9–10 to a single "and there's also auth, routing, generalization, resilience, and 2,500 lines of tests" summary slide. The loop + grounding + safety is the irreducible core of the story.
