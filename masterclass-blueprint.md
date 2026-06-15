# Masterclass Blueprint — "Atlas: Build an Agent That Uses Any App Like a Human"

**Format:** 60 minutes, live — demo → teach → **build live** → safety → pitch → Q&A
**Audience:** SDEs and technical generalists evaluating our Agentic AI course
**Goal:** Make them *feel* the wow, *watch* the engineering get built in front of them, and *believe they could build it* — then convert to the course.

> **What changed in this version:** the talk is no longer demo-and-teach only. The
> centerpiece is now a **live build** — we code the bare agent loop from scratch on
> stage, run it, then reveal the production harness around it. The shipped feature set
> is also much wider than the original deck: cross-app comparison, saved & scheduled
> tasks, the redesigned "Mission Control" dashboard, and Google Workspace cloud actions
> (Gmail send + Meet scheduling). Those become the "where this goes" proof points.

---

## 0. The one sentence everything hangs on

> **"The AI isn't the hard part. The engineering around the AI is — and that's a learnable craft."**

Every wow moment proves the agent is real; every teaching and build moment proves the
*engineering* (grounding, guards, safety, recovery, orchestration) is what separates a
toy demo from something you'd let touch your credit card. **The course teaches that
craft. The agent is just the vehicle.**

**Secondary throughline (positioning):** *You can't beat the native app on raw speed.
You win where native can't go* — hands-free voice, any-app generality, no integrations,
human-gated safety, scheduled/cross-app/cloud composition. Keep returning to "where
native can't go."

---

## 1. Audience psychology (design the whole hour around this)

| They walk in thinking… | We must move them to… |
|---|---|
| "Another GPT-wrapper demo." | "This drives a *real phone*, no API. That's different." |
| "Cool, but it's a fragile toy." | "There's a guard stack, a HITL gate, audit logs — this is engineered." |
| "I could never build this." | "He just built the loop in 5 minutes on stage. I *could* build this." |
| "Why pay for a course?" | "The gap between his 30-line loop and the production harness is exactly the curriculum." |

Two sub-audiences, one talk — alternate every few minutes so neither drifts:
- **SDEs** want the *architecture* ("no per-app integration?! how?") → feed them the loop, grounding, the guard stack, the live build.
- **Generalists** want the *magic* ("I talked to my phone and it ordered groceries") → feed them voice, hands-free, cross-app, "where native can't go."

---

## 2. The arc at a glance (five acts)

| Act | Minutes | Beat | The job it does |
|---|---|---|---|
| **I — Hook** | 0:00–0:08 | Cold-open live order, pause at pay | Wow #1. Earn attention before any preamble. |
| **II — Why hard / mental model** | 0:08–0:24 | Kill the 3 wrong approaches → "perceive, don't integrate" → the loop | Set up the build. Make them want the *how*. |
| **III — BUILD LIVE** | 0:24–0:40 | Code the bare loop from scratch → run it → reveal the harness | **The centerpiece.** Wow #2 = "I could build this." Conversion engine. |
| **IV — Make it real** | 0:40–0:50 | Deny-the-payment (safety) + "where native can't go" (comparison / scheduled / voice / cloud) | Wow #3 = trust. Then expand the ceiling. |
| **V — Pitch & close** | 0:50–1:00+ | Curriculum-maps-to-what-you-saw → CTA → Q&A | Convert. |

Wow peaks land at **0:00**, **~0:32** (the loop runs), and **~0:42** (deny the payment) —
roughly one every ~12 minutes. Start on a peak, end on a peak.

---

## 3. Minute-by-minute flow

### 3.1 — COLD OPEN (0:00–0:03) — *Wow #1, no preamble*

**No introduction. No agenda.** Open on a phone mirrored full-screen (scrcpy). One sentence:

> "I'm going to talk to my phone, and it's going to order groceries. I didn't write a single line of Blinkit-specific code. Watch."

Trigger live (text or voice — §6). The agent launches the app, searches, adds to cart, and **pauses at checkout asking for approval.** Let the room watch the taps happen on their own.

> 🎯 **Engagement hook:** as it runs, ask: *"Predict — what does it tap next?"* Room leans in within 90 seconds.

**Why it works:** autonomous taps on a real consumer app are visceral in a way slides never are. The unexpected *pause before paying* plants the safety seed you pay off in Act IV.

### 3.2 — THE REVEAL & PROMISE (0:03–0:08)

Introduce yourself in 10 seconds (credibility only). Reframe what they saw:

- "No Blinkit API. No web scraping. No accessibility hack. It **looked at the screen** and decided the next tap — like you would."
- The promise: *"In the next 50 minutes I'll show you how this works, then I'll build the core of it from scratch, live, in front of you — and you'll see why the hard 80% isn't the AI."*
- Drop the throughline (§0) on a slide. Leave it as a recurring anchor.

### 3.3 — WHY THIS IS HARD / WHY NOW (0:08–0:16)

Walk the three "obvious" approaches and kill each:

1. **Per-app APIs / integrations** — don't exist for most apps, break constantly, need partnerships. Doesn't scale to "any app."
2. **RPA / coordinate scripts** — brittle; one UI update and every tap is wrong.
3. **Accessibility-service automation** — invasive, app-specific, fights the OS.

**The pivot:** *Stop integrating. Perceive.* Treat the phone like a human does — look, reason, tap. One loop, every app. This is *computer use*, viable only now that vision-language models got good enough (the "why now").

> 🎯 **Engagement hook:** quick poll — *"Who's tried to automate something on their phone and given up?"* Hands up; you've named their pain.

### 3.4 — THE MENTAL MODEL (0:16–0:24)

Set up the build by drawing the target on one accreting slide. Keep it conceptual — you're about to *write* it, so don't over-explain in prose.

```
screenshot → vision model: "what's the next tap?" → tap → repeat
```

Say: *"That's the whole agent. Everything else is making this loop not embarrass you. Let me prove the first half is easy."* → transition straight into the build.

---

### 3.5 — BUILD LIVE: THE LOOP, THEN THE HARNESS (0:24–0:40) — *the centerpiece*

This is the new heart of the talk. Detailed script in **§4**. Two movements:

**Movement 1 — build the bare loop from scratch (0:24–0:32).** In a clean editor, type a ~30-line `perceive → reason → act` loop against the real device. Run it. It works (mostly). The room watches an agent *come to life* from nothing.

> 🎯 Peak: when the hand-built loop makes its first correct tap, pause. *"That's an agent. You just watched me build one. So why is there a course?"* → Movement 2.

**Movement 2 — reveal the harness (0:32–0:40).** Show what the toy loop gets wrong (hallucinated coordinates, six identical "ADD" buttons, infinite loops, no safety). Then reveal the production pieces that fix each — grounding (UI tree), the guard stack, verification, HITL — as *diffs against the toy you just wrote*. **"The model proposes; the harness disposes."**

The whole act earns the line: *the AI was the easy 20%; this harness is the 80% the course teaches.*

---

### 3.6 — MAKE IT REAL: SAFETY / HITL (0:40–0:46) — *Wow #3, the trust moment*

Pay off the checkout pause from the cold open.

- Show the **Approve / Deny** card arriving (Telegram **and** the dashboard) the instant the agent hits payment / OTP / delete / permission. Tap **Deny** live — the task stops cold.
- Walk the safety model fast: emulator-only by default, per-user policy (`read_only` / `confirm_sensitive` / `always_approve`), an allow/block validator (no calls/SMS/factory-reset/uninstall), and a **redacted audit log written before *and* after every action.**

**The line:** *"An agent you can't stop is a liability, not a product. The pause is the feature."* This silently answers the question every serious buyer is forming — *"what happens when it screws up?"* — which is what converts skeptics.

### 3.7 — WHERE NATIVE CAN'T GO (0:46–0:50) — *expand the ceiling*

Now that they trust it, show the breadth — this is where the *new* shipped features earn their place. Pick **two**, demo or show on the dashboard, name the rest:

- **Cross-app comparison** *(shipped, demoable):* "cheapest pizza on Swiggy or Zomato" → read-only price probes across both apps → ranked → confirm-then-order. *Native makes you compare in your head.*
- **Scheduled / recurring** *(shipped):* "every Sunday 9am, reorder groceries" → async scheduler, stops at cart unless you opt into auto-pay. *Native needs you to remember.*
- **Hands-free voice trigger** *(shipped):* press a phone shortcut, speak, the agent reads the intent back, you confirm, it runs. *Where your thumbs can't go — cooking, driving.*
- **Google Workspace cloud actions** *(shipped):* "email the team the cricket plan and set up a Meet for Friday 5pm" → sends Gmail + schedules a Google Meet, no phone automation at all. *The agent isn't even a phone thing anymore — it composes across cloud surfaces.*
- **The dashboard ("Atlas Mission Control"):** the live web view — see the agent's screen in a phone frame with the tapped-path overlay, history, replays, schedules calendar, analytics. *This is the "glass cockpit" over the whole system.*

> ⚠️ **Integrity note:** comparison, saved/scheduled, voice, and cloud actions are all genuinely shipped and tested. Demo what you've pre-tested *today*; **narrate the rest over the dashboard** rather than risk an untested live run. This audience respects honesty and punishes overselling.

### 3.8 — "HERE'S WHAT YOU'LL BUILD" (0:50–0:56) — *the pitch, earned*

Map the curriculum onto the exact moments they witnessed — don't pivot to a generic sales slide:

| What wowed them | What the course teaches |
|---|---|
| It ordered with no API | The perceive-reason-act loop & computer use |
| **He built the loop in 5 minutes** | **You start here too — then we make it production-grade** |
| It tapped the *right* button | Grounding: UI trees, element roles, coordinate truth |
| It didn't do anything dumb | The guard stack & self-correction |
| It paused before paying | HITL design, safety, audit, authz |
| It ran on any app | Provider abstraction & generalized harness design |
| It compared apps / scheduled / emailed | Orchestration, async, cross-surface composition |

Then: *"You walk out with your own agent driving a real device — and the engineering judgment to make it safe. That judgment is the durable, transferable skill — not the prompt."*

### 3.9 — CTA + Q&A (0:56–1:00+)

- One clear CTA slide (enroll link / offer / deadline). Say it out loud.
- A single memorable closing line restating §0.
- Open Q&A. Seed a planted first question if the room is shy: *"A common question is how often the model gets it wrong?"* → *"Constantly — that's why the guards exist,"* which re-sells the engineering.

---

## 4. THE LIVE BUILD — detailed stage script (the centerpiece)

> The goal is not to write production code on stage. It is to make the audience *feel*
> the gap between "a loop anyone can write" and "an agent you'd trust." You build the
> first; you reveal the second. **Rehearse this until it's muscle memory** — a fumbled
> live build is worse than no live build. Have a recorded run of the toy loop cued.

### Movement 1 — Build the bare loop from scratch (~8 min)

Open a fresh file, e.g. `live_loop.py`, in a large-font editor next to the mirrored phone. Type it live, narrating each line. Target ~30 lines:

```python
# live demo: the entire agent, minus everything that makes it safe
import subprocess, json
from providers import vision          # our pluggable brain (one import)

GOAL = "add milk to the cart on Blinkit"

def screenshot():
    subprocess.run(["adb", "exec-out", "screencap", "-p"], stdout=open("s.png","wb"))
    return "s.png"

def tap(x, y):
    subprocess.run(["adb", "shell", "input", "tap", str(x), str(y)])

for step in range(20):
    img = screenshot()
    action = vision.next_action(GOAL, img)     # → {"type":"tap","x":..,"y":..} | {"done":true}
    print("model says:", action)
    if action.get("done"):
        print("task complete"); break
    if action["type"] == "tap":
        tap(action["x"], action["y"])
```

**Talk track while typing:**
- *"Screenshot. Ask the model what to tap. Tap it. Loop. That is the entire idea."*
- *"The model is behind one import — Gemini today, swap it for Claude or GPT-4V with one env var. The model is a commodity. The harness is the product."* (plant the line)

**Run it.** It will (mostly) work for a step or two. Two honest outcomes, both useful:
- **It works** → *"You just watched me build an agent in thirty lines. So why is there a course? Watch what happens when I trust it."*
- **It misfires** (hallucinated coordinate, taps the wrong thing) → *"Perfect — this is the point. The model is wrong all the time. Now I'll show you the engineering that catches it."*

Either way, the transition to Movement 2 is the same: the toy is not trustworthy.

### Movement 2 — Reveal the harness as diffs against the toy (~8 min)

Don't write the production code live — *show* it beside the toy and explain each piece as the fix to a specific failure the toy has. Keep it to four reveals; cite real files.

| Toy loop's failure | The fix (reveal) | Real file |
|---|---|---|
| Model invents coordinates from a raw image | **Grounding:** feed it the UI tree — `uiautomator` → compact element list with *real* center coords, role tags (`[ACTION]`/`[CART]`/`[LOCATION]`), product title on each ADD button. "Six identical ADDs become six distinguishable choices." | `agent/orchestrator.py`, perception |
| Taps the wrong "ADD", loops forever, types into nothing | **The guard stack:** structural guards reject a bad action *before* it touches the device — coordinate grounding, intent mismatch, product-name mismatch, repeated-ADD, type-without-focus, loop detection. | `security/action_validator.py` |
| Has no idea if the tap did anything | **Verification:** re-screenshot, check the screen actually changed (perceptual hash); if stuck, back-button then escalate. "The agent notices when it's stuck." | `agent/orchestrator.py`, verify step |
| Would happily pay / delete / enter an OTP | **HITL gate:** pause the loop, push an Approve/Deny card, await a human. | `security/hitl_gate.py` |

**The payoff line (the most persuasive moment in the talk):**
> *"Everything I typed from scratch was the easy twenty percent. Everything I just revealed — grounding, guards, verification, the human gate — is the eighty percent. That eighty percent is the course."*

This is the structural reason the live-build beats a pure demo: the audience now has a
*personal* reference point (the loop they watched you write) against which the course's
value is measured. The gap is no longer abstract.

> **Fallback if the live edit is too risky for the room/time:** keep Movement 1 but
> paste a pre-written `live_loop.py` and walk it line-by-line instead of typing. You
> lose a little theater, keep all the pedagogy. Decide same-day based on the network and
> your nerves.

---

## 5. Feature showcase map — shipped vs. roadmap (be honest)

| Capability | Status | Use it in the talk as… |
|---|---|---|
| Telegram text/voice → order, pause at checkout | 🟢 Shipped, solid | Cold open (§3.1), deny demo (§3.6) |
| Generalizes to any app (no per-app code) | 🟢 Shipped, solid | The whole premise; harness reveal (§4) |
| Guard stack, HITL, audit, validator | 🟢 Shipped, solid | Harness reveal + safety (§4, §3.6) |
| Cross-app price comparison | 🟢 Shipped, tested live | "Where native can't go" (§3.7) |
| Saved quick tasks / `/run` | 🟢 Shipped | Mention; show on dashboard |
| Scheduled / recurring tasks | 🟢 Shipped | "Where native can't go" (§3.7) |
| Hands-free voice trigger | 🟢 Shipped (OEM/mic quirks) | Demo only if pre-tested same day |
| Google Workspace (Gmail send + Meet schedule) | 🟢 Shipped, tested | "It's not even a phone thing anymore" (§3.7) |
| Atlas Mission Control dashboard | 🟢 Shipped (redesigned) | The glass cockpit — narrate breadth over it |
| Multi-app *in one instruction*, watch/car surfaces | 🔴 Roadmap | Frame as "where this goes," never fake |

---

## 6. Demo reliability tiers (what to trust live)

| Demo | Tier | Live? | Backup |
|---|---|---|---|
| Telegram text → order on Blinkit, pause at checkout | 🟢 Solid | Yes | Recording |
| **The live-build loop (Movement 1)** | 🟢 if rehearsed | Yes | Pre-written file + recorded run |
| Run same loop on a 2nd pre-installed app | 🟢 Solid | Yes | Recording |
| Deny payment via Telegram/dashboard card | 🟢 Solid | Yes | Recording |
| Cross-app comparison | 🟡 Medium (fuzzy prices) | Yes if pre-tested today | Recording + dashboard view |
| Hands-free voice trigger | 🟡 Medium (mic/STT/OEM) | Only if pre-tested same day | **Recording mandatory** |
| Google Workspace email + Meet | 🟡 Medium (OAuth/network) | Yes if pre-tested today | Recording |
| Audience-chosen *arbitrary* app | 🟡 Medium | From a vetted shortlist | Fall back to 2nd app |
| Scheduled run firing live | 🔴 Hard to time | No | Dashboard schedules calendar + concept |

**Iron rule:** every live demo has a pre-recorded capture cued up. If anything stalls
>15s, narrate over the recording and move on — **never debug live.** A gracefully
recovered failure is fine; a 90-second silent stall kills the room.

---

## 7. Pre-flight checklist (run 30 min before)

- [ ] Bot launched with **`.venv/bin/python -u main.py`** (not bare Python — silent import failures).
- [ ] `curl -s http://127.0.0.1:8765/health` → `{"ok": true}`
- [ ] `adb reverse --list` shows `tcp:8765` (voice webhook).
- [ ] `adb devices` → exactly the intended device; `ALLOW_PHYSICAL_DEVICE=1` if on a real phone.
- [ ] Dashboard up at `:8770`; `npm run build` done so `dist/` is current; token in localStorage; Live-run view rendering (EventSource connected — remember SSE shows no access-log line).
- [ ] Provider = `openrouter` (Router/intent classifier active) and **quota/RPD headroom checked** — a quota wall mid-demo is the #1 silent killer.
- [ ] `SESSION_TIMEOUT_SECONDS=600` (not the 180 default) so a slow network doesn't time out.
- [ ] **Live-build editor ready:** large font, `live_loop.py` scratch file open, `providers` import path working, pre-written fallback file in an adjacent tab, recorded toy-loop run cued.
- [ ] Google OAuth connected (Settings page) **if** demoing Workspace; test send to a safe address.
- [ ] scrcpy mirroring at back-row-legible size; phone **Do Not Disturb on**, brightness up, auto-lock off, lockscreen unlocked.
- [ ] App state pre-staged: logged in, address set, cart empty, payment method present (checkout reachable but gated).
- [ ] Test the *exact* utterances/messages, on today's network, end-to-end.
- [ ] Shortlist of 2–3 vetted "audience-choice" apps confirmed installed & logged in.
- [ ] All backup recordings open in tabs, cued to start. Telegram on the projected machine, chat cleared, font up.

---

## 8. Exact demo scripts (grounded in what's built)

**A — text order (cold open):** Telegram `order milk on blinkit` → launches → searches → adds → `need_approval` "Cart review" → Approve/Deny card. Track: *"no Blinkit code… watch it ground each tap… and — it stopped. It won't pay without me."*

**B — generalization (audience app):** from the vetted list — `search for biryani on swiggy` / `drop a pin for the airport in maps` / `draft a message to Mom on whatsapp`. Track: *"same loop, zero new code."*

**C — deny payment:** re-run A, tap **Deny** on the card (Telegram or dashboard). Track: *"task stops, logged, nothing charged. The pause is the feature."*

**D — voice (if green-lit by same-day test):** `POST /trigger {"text":"okay atlas, add milk on blinkit"}` → agent speaks back "Got it — add milk on Blinkit. Confirm?" → confirm → it runs. Track: *"hands-free — where your thumbs can't go."*

**E — cross-app comparison:** `cheapest pizza on swiggy or zomato` → read-only probes both → ranked quotes (e.g. Swiggy ₹129 beat Zomato ₹369) → confirm-then-order. Track: *"it compared two apps for me. Native makes you do that in your head."*

**F — cloud action (if green-lit):** `email the team this week's plan and set up a Meet Friday 5pm` → Gmail sent + Meet scheduled, no phone automation. Track: *"that wasn't even on the phone — the agent composes across surfaces."*

---

## 9. Slide list (lean — demos and the build carry the weight)

1. *(none — cold open on the phone)*
2. Title + the one sentence (§0)
3. The promise / what you'll leave with — **including "I'll build it live"**
4. Three approaches that fail (§3.3)
5. The pivot: perceive, don't integrate
6. The loop — animated, accreting (§3.4) → **then switch to the editor for the live build**
7. *(live build — editor + phone, no slide)*
8. Harness reveal: the 4 fixes as diffs (§4, Movement 2)
9. Safety model (emulator-only · authz · validator · HITL · audit)
10. Where native can't go (comparison · scheduled · voice · cloud · dashboard)
11. Curriculum-maps-to-wow table (§3.8)
12. CTA

Keep slides sparse: one idea each. The phone and the editor are the real deck.

---

## 10. Failure-recovery talk tracks (memorize)

- **Live build won't run / typo:** *"This is real code in front of a real room — give me one second. And notice: even getting the loop to run is fiddly. Now imagine making it safe."* Then paste the fallback file. Never debug >20s — switch to walking the pre-written version.
- **Demo stalls:** *"While this thinks — this is exactly why we built loop detection and a timeout. Here's the run I captured this morning."* → recording.
- **Wrong tap live:** *"Perfect — this is the point. The model is wrong sometimes; watch the guard catch it / watch it back out and retry."* Turn the failure into the §4 lesson — a recovered failure converts better than a clean run.
- **Quota / provider error:** *"I've hit a rate limit — a real production concern we handle with throttling. Here's the recording."* Never debug live.

---

## 11. Structural note: bookend the same task

Consider bookending: order in the cold open (pause at pay), and in the safety segment
*complete that same paused order* with an Approve. The narrative closes a loop the
audience opened with you — it makes the 50-minute span feel like one continuous story
rather than disconnected demos.

---

## 12. Next step — producing the deck

This blueprint is the content spec. The deck (`masterclass-deck.html`, reveal.js) is
built *from* it. When you green-light the content:
- Horizontal spine = the five-act beginner narrative (§2–§3); press **↓** for engineer
  deep-dives (the loop internals, grounding, the guard list, the dashboard architecture).
- Mermaid diagrams must render **lazily on the visible slide** and use **ASCII-only**
  labels (see the deck regression notes). Talk-track/timing in `<aside class="notes">`.
- The live-build act (§4) is **not** slides — it's the editor + phone. The deck only
  bookends it (slide 6 → build → slide 8).

**Recommended order from here:** (1) you review/adjust this blueprint, (2) I rebuild
`masterclass-deck.html` professionally to match it, (3) we rehearse the live-build script
against the real device and cut the backup recordings.
