# Masterclass Blueprint — "Atlas: Build an Agent That Uses Any App Like a Human"

**Format:** 60 minutes, live, end-to-end demo + teaching
**Audience:** SDEs and technical generalists (people who can code, evaluating the course)
**Goal of the session:** Make them *feel* the wow, *understand* the engineering, and *believe they could build it* — then convert to the course.

---

## 0. The one sentence everything hangs on

> **"The AI isn't the hard part. The engineering around the AI is — and that's a learnable craft."**

Everything in the hour is in service of this. Every wow moment proves the agent is real; every teaching moment proves the *engineering* (grounding, guards, safety, recovery) is what separates a toy demo from something you'd let touch your credit card. The course teaches that craft. The agent is just the vehicle.

**Secondary throughline (the positioning, from our strategy):** *You can't beat the native app on raw speed. You win where the native app can't go* — hands-free voice, any-app generality, no integrations, human-gated safety. Keep returning to "where native can't go."

---

## 1. Audience psychology (design the whole hour around this)

| They walk in thinking… | We must move them to… |
|---|---|
| "Another GPT-wrapper demo." | "This drives a *real phone*, with no API. That's different." |
| "Cool, but it's a fragile toy." | "There are 9 guards, a HITL gate, audit logs — this is engineered." |
| "I could never build this." | "It's a loop: see → think → act. I *could* build this." |
| "Why pay for a course?" | "The gap between my toy and this is exactly the curriculum." |

Two sub-audiences, one talk:
- **SDEs** want the *architecture* ("no per-app integration?! how?") → feed them the loop, the grounding, the guard stack.
- **Generalists** want the *magic* ("I talked to my phone and it ordered groceries") → feed them voice, hands-free, "where native can't go."

Alternate between these every few minutes so neither group drifts.

---

## 2. The minute-by-minute flow

> Timings assume a 60-min slot with ~7 min Q&A at the end. Adjust the frontier block (§2.7) if running long — it's the designated "cut for time" segment.

### 2.1 — COLD OPEN (0:00–0:03) — *Wow #1, no preamble*

**Do not introduce yourself. Do not show an agenda.** Open on a phone screen mirrored full-screen (scrcpy). Say one sentence:

> "I'm going to talk to my phone, and it's going to order groceries. I did not write a single line of Blinkit-specific code. Watch."

Then trigger live (text or voice — see demo tier in §4). The agent launches the app, searches, adds to cart, and **pauses at checkout asking for approval.** Let the room watch the taps happen on their own.

**Why this works:** autonomous taps on a real consumer app are visceral in a way slides never are. The unexpected *pause before paying* plants the safety seed immediately — and is the hook you'll pay off in §2.6.

> 🎯 **Engagement hook:** as it's running, ask the room: *"Predict — what does it tap next?"* Gets them leaning in within 90 seconds.

### 2.2 — THE REVEAL & PROMISE (0:03–0:08)

Now introduce yourself (10 seconds, credibility only). Then reframe what they just saw:

- "No Blinkit API. No web scraping. No accessibility-service hack. It **looked at the screen** and decided the next tap — the same way you would."
- State the promise: *"In the next 50 minutes I'll show you exactly how this works, run it live on an app I'll pick from the audience, and show you why the hard 80% isn't the AI."*
- Drop the throughline (§0) on a slide. Leave it up as a recurring anchor.

### 2.3 — WHY THIS IS HARD / WHY NOW (0:08–0:15)

The tension-builder. Walk the three "obvious" approaches and kill each:

1. **Per-app APIs / integrations** — don't exist for most apps, break constantly, need partnerships. Doesn't scale to "any app."
2. **RPA / coordinate scripts** — brittle; one UI update and every tap is wrong.
3. **Accessibility-service automation** — invasive, app-specific, fights the OS.

**The insight (the pivot):** *Stop integrating. Perceive.* Treat the phone like a human does — look, reason, tap. One loop, every app. This is *computer use*, and it only became viable when vision-language models got good enough (the "why now").

> 🎯 **Engagement hook:** quick poll — *"How many of you have tried to automate something on your phone and given up?"* Hands go up; you've named their pain.

### 2.4 — THE MENTAL MODEL + ARCHITECTURE (0:15–0:28)

The core teaching block. Build the loop on screen, layer by layer — ideally one slide that accretes boxes as you talk. Map each layer to the README so it's concrete, not hand-wavy.

**Layer 1 — the bare loop (the "aha"):**
```
screenshot → vision model: "what's the next tap?" → tap → repeat
```
Say: *"That's it. That's the whole agent. Everything else is making this loop not embarrass you."*

**Layer 2 — Perception (grounding):** a raw screenshot isn't enough — the model hallucinates coordinates. So we feed it the **UI tree**: `uiautomator` → a compact element list with *real* center coordinates, role tags (`[ACTION]`, `[CART]`, `[LOCATION]`), and the product-card title attached to each ADD button. *"Six identical 'ADD' buttons become six distinguishable choices."* This is the SDE catnip — grounding is the difference between 40% and 95%.

**Layer 3 — Decision (pluggable brain):** the vision model is behind a `Protocol` — Gemini, Vertex, or any OpenRouter model, swapped via one env var. No framework, no LangGraph — a hand-written async state machine. *"The model is a commodity you slot in. The harness is the product."*

**Layer 4 — Action & verification:** tap/type/swipe over ADB; then **re-screenshot and check the screen actually changed** (perceptual hash). If nothing changed, escalate — back-button, then fail. *"The agent notices when it's stuck."*

**Layer 5 — the guard stack (the credibility payload):** nine structural guards that reject a bad action *before it touches the device* — coordinate grounding, intent mismatch, product-name mismatch, repeated-ADD, type-without-focus, loop detection. *"The model is wrong all the time. The engineering is catching it."*

> This block is where SDEs decide the course is legit. Don't rush it. The single most persuasive line in the whole talk: **"The model proposes; the harness disposes."**

### 2.5 — WOW BLOCK: GENERALIZATION (0:28–0:38) — *Wow #2, the centerpiece*

This is the strongest, most defensible wow. The architecture genuinely has no per-app flows.

- **Take an app suggestion from the audience** (constrain to a few pre-installed safe ones — see §5). Run the *same loop* on it live: a food app, a maps search, a chat draft.
- Narrate: *"Same code. Same loop. I added zero lines for this app. The only per-app thing is an optional markdown 'skill' file of hints — and even without it, it works."*

**The point that lands:** generality isn't a feature you code per app; it's a property of the architecture. That reframing is worth the whole segment.

> 🎯 **Engagement hook:** let the audience pick the *task* too ("what should it do in this app?"). Their ownership of the prompt makes the success feel un-staged.

### 2.6 — THE THING THAT MAKES IT REAL: SAFETY / HITL (0:38–0:46) — *Wow #3, the trust moment*

Pay off the checkout pause from the cold open. This is what separates "scary demo" from "something I'd actually use."

- Show the **Approve / Deny card** arriving in Telegram the instant the agent hits a payment / OTP / delete / permission screen. Tap **Deny** live — watch the task stop cold.
- Walk the safety model fast: emulator-only by default, per-user policy (`read_only` / `confirm_sensitive` / `always_approve`), an allow/block validator (no calls/SMS/factory-reset/uninstall), and a **redacted audit log written before *and* after every action.**

**The line:** *"An agent you can't stop is a liability, not a product. The pause is the feature."*

This segment quietly answers the objection every serious person in the room is forming: *"What happens when it screws up?"* You've shown them. That's trust, and trust is what converts skeptics.

### 2.7 — FRONTIER: WHERE NATIVE CAN'T GO (0:46–0:52) — *cut-for-time segment*

Tie back to positioning. Be honest about what's shipped vs. roadmap (credibility > hype with this audience).

- **Shipped & demoable:** hands-free **voice trigger** — press a phone shortcut, speak, the agent reads the intent back, you confirm, it runs. No screen, no typing. *"This is the thing your thumbs can't do while you're cooking/driving."*
- **Roadmap (frame as vision, don't fake it):** scheduled/proactive runs, cross-app orchestration, multi-surface (watch, car). Say plainly: *"Here's the frontier — and in the course you build toward it."*

> ⚠️ Integrity note: single-task voice/text + HITL is solid and live-safe. True multi-app-in-one-instruction and scheduling are roadmap — present them as *where this goes*, not as working demos. This audience will respect the honesty and punish overselling.

### 2.8 — "HERE'S WHAT YOU'LL BUILD" (0:52–0:57) — *the pitch, earned*

Bridge directly from what they just saw. Don't pivot to a generic sales slide — map the curriculum onto the exact moments they witnessed:

| What wowed them | What the course teaches |
|---|---|
| It ordered with no API | The perceive-reason-act loop & computer use |
| It tapped the *right* button | Grounding: UI trees, element roles, coordinate truth |
| It didn't do anything dumb | The guard stack & self-correction |
| It paused before paying | HITL design, safety, audit, authz |
| It ran on any app | Provider abstraction & generalized harness design |

Then: *"You walk out with your own agent driving a real device — and the engineering judgment to make it safe."* That judgment is the durable, transferable skill — not the prompt.

### 2.9 — CTA + Q&A (0:57–1:00+)

- One clear CTA slide (enroll link / offer / deadline). Say it out loud, don't just show it.
- A single memorable closing line that restates the throughline (§0).
- Open Q&A. Seed it with a planted first question if the room is shy (*"A common question is: how often does the model get it wrong?"* → great answer: "constantly — that's why the guards exist," which re-sells the engineering).

---

## 3. Wow-moment engineering (the meta-skill of this talk)

Three planned peaks: **cold-open order (0:00)**, **unseen-app generalization (0:28)**, **deny-the-payment (0:38)**. Spacing matters — a peak roughly every ~12 minutes keeps energy up. Rules:

1. **Start on a peak, end on a peak.** Never open with logistics.
2. **Make the audience co-author the wow** (they pick the app/task) — co-authored success can't be dismissed as staged.
3. **Narrate the autonomy.** Say what it's about to do *before* it does it, so the room scores the agent live.
4. **The pause is a peak too.** Counterintuitively, stopping the agent is more impressive to serious buyers than completing the task.

---

## 4. Demo reliability tiers (what to trust live)

| Demo | Tier | Live? | Backup |
|---|---|---|---|
| Telegram text → order on Blinkit, pause at checkout | 🟢 Solid | Yes | Recording |
| Run same loop on a 2nd pre-installed app | 🟢 Solid | Yes | Recording |
| Deny payment via Telegram card | 🟢 Solid | Yes | Recording |
| Hands-free voice trigger (shortcut → speak → confirm) | 🟡 Medium (mic/STT/OEM quirks) | Yes if pre-tested same day | **Recording mandatory** |
| Audience-chosen *arbitrary* app | 🟡 Medium | Yes, from a vetted shortlist | Fall back to your 2nd app |
| Scheduled / cross-app | 🔴 Roadmap | **No** | Slide / concept only |

**Iron rule:** every live demo has a pre-recorded screen capture cued up. If anything stalls >15s, narrate over the recording and move on — *never* debug live. A failed demo you recover from gracefully is fine; a 90-second silent stall kills the room.

---

## 5. Pre-flight checklist (run 30 min before)

- [ ] Bot launched with **`.venv/bin/python -u main.py`** (not bare Python — it silently fails imports).
- [ ] `curl -s http://127.0.0.1:8765/health` → `{"ok": true}`
- [ ] `adb reverse --list` shows `tcp:8765` (if using the voice webhook).
- [ ] `adb devices` → exactly the intended device; `ALLOW_PHYSICAL_DEVICE=1` if on a real phone.
- [ ] Provider = `openrouter` (so the **Router** is active and intents resolve cleanly) and **quota/RPD headroom checked** — a quota wall mid-demo is the #1 silent killer.
- [ ] `SESSION_TIMEOUT_SECONDS=600` deployed (not the 180 default) so a slow demo network doesn't time out.
- [ ] scrcpy mirroring at a back-row-legible size; phone **Do Not Disturb on**, brightness up, auto-lock off.
- [ ] App state pre-staged: logged in, address set, cart empty, payment method present (so checkout is reachable but gated).
- [ ] Test the *exact* utterances/messages you'll use, on today's network, end-to-end.
- [ ] Shortlist of 2–3 vetted "audience-choice" apps confirmed installed & logged in.
- [ ] All backup recordings open in tabs, cued to start.
- [ ] Telegram on the projected machine, chat cleared, font size up.

---

## 6. Exact demo scripts (grounded in what's built)

**Demo A — text order (cold open):**
- Telegram message: `order milk on blinkit`
- Expected: launches Blinkit → searches → adds → `need_approval` "Cart review" → Approve/Deny card.
- Talk track: "no Blinkit code… watch it ground each tap… and — it stopped. It won't pay without me."

**Demo B — generalization (audience app):**
- From vetted list, e.g.: `search for biryani on swiggy` / `drop a pin for the airport in maps` / `draft a message to Mom on whatsapp`
- Talk track: "same loop, zero new code."

**Demo C — deny payment:**
- Re-run Demo A, tap **Deny** on the card.
- Talk track: "task stops, logged, nothing charged. The pause is the feature."

**Demo D — voice (if green-lit by same-day test):**
```
POST /trigger  {"text": "okay atlas, add milk on blinkit"}
→ agent speaks back "Got it — add milk on Blinkit. Confirm?"
→ confirm → it runs
```
- Talk track: "hands-free — where your thumbs can't go."

---

## 7. Slide list (lean — demos carry the weight)

1. *(none — cold open on the phone)*
2. Title + the one sentence (§0)
3. The promise / what you'll leave with
4. Three approaches that fail (§2.3)
5. The pivot: perceive, don't integrate
6. The loop — animated, accreting (§2.4)
7. Grounding: a real UI-tree element list
8. The guard stack (the 9, one line each)
9. Safety model (emulator-only · authz · validator · HITL · audit)
10. Frontier / where native can't go
11. Curriculum-maps-to-wow table (§2.8)
12. CTA

Keep slides sparse: one idea each. The phone is the real slide deck.

---

## 8. Failure-recovery talk tracks (memorize these)

- **Demo stalls:** "While this thinks — this is exactly why we built loop detection and a timeout. Let me show you the run I captured this morning." → cut to recording.
- **Wrong tap live:** "*Perfect* — this is the point. The model is wrong sometimes; watch the guard catch it / watch it back out and retry." (Turn the failure into the §2.4 lesson — a recovered failure is more convincing than a clean run.)
- **Quota / provider error:** "I've hit a rate limit — which, by the way, is a real production concern we handle with throttling. Here's the recording." → never debug live.

---

## 9. If you want one structural upgrade

Consider **bookending with the same task**: order in the cold open (pause at pay), and in the safety segment *complete* that same paused order with an Approve. The narrative closes a loop the audience opened with you — satisfying, and it makes the 40-minute span feel like one continuous story rather than disconnected demos.
