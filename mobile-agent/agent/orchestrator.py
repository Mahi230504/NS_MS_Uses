"""Master agent: receives task, runs loop, reports back."""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable, Optional

_log = logging.getLogger("mobile_agent.orchestrator")

from agent.action_executor import execute as execute_action
from agent.persistence import TaskRepository
from agent.phash import compute as compute_phash
from agent.profiles import COMMERCE, AppProfile
from agent.providers.base import VisionProvider
from config import prompts
from agent.skills import SkillRegistry
from agent.state_machine import Task, TaskState
from agent.ui_tree import (
    UiElement,
    find_action_at,
    find_smallest_element_at,
    has_focused_text_input,
    is_coord_in_elements,
    parse as ui_tree_parse,
    render_elements as ui_tree_render,
)
from bot.users import UserStore
from device.adb_controller import KEYCODE_BACK, AdbController, AdbError
from security.action_validator import validate
from security.audit_logger import AuditLogger
from security.hitl_gate import HitlGate, ReadOnlyViolation


MAX_LOOP_ITERATIONS = 30
# A read-only price probe (cross-app comparison) should be SHORT — search, open
# the result, read the price, report. Cap it below the full order budget so a
# 3-app comparison can't run ~3x30 steps. Headroom note: a COLD app start burns
# several steps on its splash/load before search is usable (a live run lost a
# whole probe to Zomato's red splash at 14), so this is set above the warm-path
# minimum (~8) to absorb a cold start while staying well under 30.
MAX_PROBE_ITERATIONS = 20
# How many times a probe is nudged to finish properly (emit a `report` after
# actually opening a result) before we accept whatever it gives. Bounded so a
# stubborn model can't loop the probe forever.
MAX_PROBE_NUDGES = 2
# Only the most recent N history entries are sent to the model each step.
# Loop/giveup detection still scans the FULL task.history orchestrator-side;
# the model simply doesn't need the entire transcript re-tokenised every step.
# Sending it whole grows the prompt — and thus per-step latency and token cost
# — roughly quadratically over a long task (each step re-sends all prior
# steps, and rejection hints are verbose). The recent tail carries what the
# model actually needs: its last few actions and any rejection it must correct.
PROMPT_HISTORY_WINDOW = 12
# After this many consecutive empty-dump steps (following at least one good
# tree), the stale-tree guard stops rejecting and lets the model act on the
# screenshot alone. Prevents an infinite reject loop on screens where
# uiautomator never reaches idle (animated cart / product-detail pages).
# Two rejections give the screen a chance to settle; the third attempt
# goes through.
MAX_STALE_TREE_REJECTS = 2
# Minimum gap between mid-loop step status messages sent to Telegram. Lifecycle
# messages (starting / done / failed / timeout) bypass this throttle.
STEP_STATUS_MIN_INTERVAL_SECONDS = 2.0
# Regexes for the "model gives up" need_approval reasons. The orchestrator
# treats matching reasons as an illegal escape (rule 10 in the system prompt)
# and rejects them unless the task history shows the model actually scrolled.
# Each pattern is matched case-insensitively against the reason string.
# Note: there is NO textual "after scrolling" bypass — earlier versions of
# this guard accepted that suffix as proof of scrolling, but the model
# learned to append it without actually scrolling. Only an executed swipe in
# task.history counts.
_GIVEUP_PATTERNS = (
    # "no <anything> products found" / "no products found" / "no Maggi found"
    re.compile(r"\bno\b[^.]{0,40}\b(products?|results?|items?|matches?)\b", re.IGNORECASE),
    re.compile(r"\bno\s+\w+\s+found\b", re.IGNORECASE),  # "no Maggi found"
    re.compile(r"\bcouldn'?t\s+find\b", re.IGNORECASE),
    re.compile(r"\bcould\s+not\s+find\b", re.IGNORECASE),
    re.compile(r"\bnothing\s+(found|matches?|matching)\b", re.IGNORECASE),
    re.compile(r"\bnot\s+available\b", re.IGNORECASE),
)
# Tokens that indicate the most recent tap was supposed to add a product to
# the cart. If the model claims that, then immediately emits a giveup-style
# need_approval, the two statements are contradictory — block the escape.
_ADD_TAP_NOTE_RE = re.compile(
    r"\b(add\s+to\s+cart|tap\s+add|ADD\b|\+\s|qty|quantity|increment|buy\s+now)",
    re.IGNORECASE,
)
# Intent regexes for the intent-vs-element check. They scan the tap's `note`
# field to infer what the model THINKS it's tapping, then we cross-check
# against the actual element under the coord.
#
# Search intent: the note mentions a search bar / box / input / field — but
# NOT an address/location word, because some apps overlap (e.g. "search for
# an address" is a legitimate location-picker action).
_SEARCH_INTENT_RE = re.compile(
    r"\b(search\s+(bar|box|input|field|icon)|search\s+for|search\s+the)\b",
    re.IGNORECASE,
)
_ADDRESS_INTENT_RE = re.compile(
    r"\b(address|location|deliver(?:y|ing)?\s|pin\s?code|change\s+pin)\b",
    re.IGNORECASE,
)
# ADD/cart intent: the model claims the tap is adding to cart, incrementing
# quantity, or starting checkout. The target MUST be an [ACTION] element.
_ADD_INTENT_RE = re.compile(
    r"\b(tap\s+add|press\s+add|add\s+to\s+cart|add\s+button|"
    r"\+\s?button|plus\s+button|increment|qty|quantity\s+(\+|plus)|"
    r"checkout|place\s+order|proceed\s+to|buy\s+now|pay\s+now)\b",
    re.IGNORECASE,
)
# Cart/commit-intent note patterns forbidden during a READ-ONLY probe (cross-app
# comparison). Broader than _ADD_INTENT_RE on purpose: it must also catch
# non-commerce commits — booking/confirming/requesting a ride — because mobility
# apps resolve to GENERIC (no [ACTION] tags), so the structural element check
# can't see them and the note is the only signal. Used by
# Orchestrator._readonly_violation_rejection.
_READONLY_FORBIDDEN_NOTE_RE = re.compile(
    r"\b(add\s+to\s+cart|tap\s+add|press\s+add|add\s+button|\+\s?button|"
    r"plus\s+button|increment|buy\s+now|buy\b|checkout|check\s*out|"
    r"place\s+(the\s+)?order|order\s+now|proceed\s+to|pay\s+now|pay\b|"
    r"confirm\s+(booking|order|ride|trip|payment|purchase)|"
    r"book\s+(the\s+)?(ride|cab|trip|now)|request\s+(ride|trip|now)|"
    r"schedule\s+(ride|trip))\b",
    re.IGNORECASE,
)
# Reasons that represent a LEGITIMATE sensitive handoff to the user — cart
# review, payment, OTP, missing payment method, address confirmation,
# permission/delete dialogs. These must reach the user even when they happen
# inside the post-loop window (otherwise we kill a real cart-review just
# because the model wobbled 2 steps earlier).
_LEGITIMATE_SENSITIVE_RE = re.compile(
    r"\b(cart\s+review|review\s+cart|"
    r"payment|pay\s+now|place\s+order|"
    r"otp|verification\s+code|verify\s+(otp|pin|code)|"
    r"no\s+payment\s+method|"
    r"confirm\s+(location|address|delivery)|"
    r"address\s+confirmation|"
    r"delete|uninstall|"
    r"permission\s+(dialog|request)?)\b",
    re.IGNORECASE,
)
# Benign-NAVIGATION need_approval reasons — the model asking PERMISSION to do
# something it should just do (press back, go home, tap the cart). Production
# finding: with no [CART] element the model emitted need_approval "...should I
# proceed with going back to the home screen?" and stalled the user. That's a
# misuse of need_approval (rule 10 reserves it for payment/OTP/delete/
# permission/cart-review). We reject these — but ONLY when the reason does NOT
# also match _LEGITIMATE_SENSITIVE_RE, so a genuine sensitive handoff is never
# blocked.
_NAV_ESCAPE_RE = re.compile(
    r"\b(go(ing)?\s+back|press\s+(the\s+)?back|navigate\s+back|"
    r"back\s+to\s+(the\s+)?home|return\s+to\s+(the\s+)?home|home\s+screen|"
    r"can('?t|not)\s+find\s+the\s+cart|cart\s+(is\s+)?not\s+visible|"
    r"no\s+['\"]?view\s+cart['\"]?|no\s+\[?cart\]?\s+(element|button|bar|icon)|"
    r"should\s+i\s+(proceed|go|navigate|tap|press|swipe|scroll)|"
    r"confirm\s+if\s+i\s+should|shall\s+i|may\s+i\b)",
    re.IGNORECASE,
)
# Cart-review reasons specifically — used to (a) detect when the user just
# approved a cart-review HITL so we can latch payment_pre_approved, and (b)
# distinguish "this is the cart screen handoff" from other sensitive flows.
_CART_REVIEW_RE = re.compile(
    r"\bcart\s+review\b|\breview\s+cart\b",
    re.IGNORECASE,
)
# Payment-flow reasons — Pay Now / Place Order / Proceed to checkout — that
# can auto-grant after a combined cart+payment approval has been latched on
# the Task. OTP and "no payment method" are intentionally NOT in this set:
# OTP must always reach the user, and no-payment-method needs a separate
# decision.
_PAYMENT_FLOW_RE = re.compile(
    r"\b(payment|pay\s+now|place\s+order|proceed\s+to\s+(payment|checkout))\b",
    re.IGNORECASE,
)
# Sensitive categories that must NEVER auto-grant, even for an unattended
# scheduled run that opted into auto-pay. These still require a human (and will
# time out safely under the bounded approval window). Used by _gate_with_hitl's
# auto_approve_payment branch.
_NON_AUTOPAY_RE = re.compile(
    r"\b(otp|verification\s+code|verify\s+(otp|pin|code)|2fa|two-factor|"
    r"no\s+payment\s+method|permission|delete|uninstall|factory\s+reset)\b",
    re.IGNORECASE,
)
# Text/desc tokens that indicate the current screen IS the cart-review
# screen — used to validate that "Cart review" need_approval reasons are
# emitted at the right moment, not while still on search-results. Any one
# match is sufficient. Case-insensitive substring search over UI text/desc.
_CART_SCREEN_TOKENS = (
    "proceed to checkout",
    "proceed to pay",
    "place order",
    "pay now",
    "select payment",
    "delivery in",
    "delivery charge",
    "bill total",
    "grand total",
    "to pay",
    "subtotal",
    "missing cart items",
    "your cart",
    "cart total",
)
# Text/desc tokens that mark the current screen as a search/results page
# where a "Cart review" need_approval would be premature — model needs to
# tap [CART] or navigate to the cart screen first. Presence + absence of
# cart-screen tokens together = premature.
_SEARCH_SCREEN_TOKENS = (
    "search for atta",
    "search for ",
    "filters",
    "sort by",
    "recent searches",
    "trending in your city",
    "people also bought",
    "see more like this",
)
# Used by the same-coords-ADD-repeat check: extract the product-name fragment
# from a note like "tap ADD on Maggi 2-Minute Masala Noodles card" so we can
# tell whether two consecutive ADD taps claim the same or different products.
_ADD_NOTE_PRODUCT_RE = re.compile(
    r"\b(?:tap\s+add\s+on|press\s+add\s+on|add\s+to\s+cart\s+for|"
    r"add\s+to\s+cart\s*[:,-]?)\s*(.+?)(?:\s+card|\s+button|\s*$)",
    re.IGNORECASE,
)
# Strict "this note is about a fresh ADD" marker — requires the literal
# word "add". Used by repeated-ADD rejection to distinguish a fresh ADD
# tap from a stepper-bump ("tap + on Maggi" / "tap qty + button"). After
# one ADD lands the same coords become the stepper; a second tap noted as
# "ADD" can't be right — it's either a redundant retry, a qty bump
# mis-labelled, or a hallucination.
_FRESH_ADD_NOTE_RE = re.compile(r"\badd\b", re.IGNORECASE)
# A tap whose note describes a quantity-INCREASING action — either a fresh ADD
# (reaches qty 1) or a stepper bump (+/plus/increment/increase). Used by the
# excess-quantity guard to count how many units the model has tried to add.
# Word-boundaried so "address" doesn't match "add". "+" is matched literally.
_QTY_INCREASE_NOTE_RE = re.compile(
    r"(\badd\b|\badd to cart\b|\bplus\b|\bincrement\b|\bincrease\b|"
    r"\bone more\b|\banother\b|\+)",
    re.IGNORECASE,
)
# Words → integer for parsing an explicit requested quantity from the task.
_QTY_WORDS = {
    "one": 1, "a": 1, "an": 1, "single": 1, "two": 2, "couple": 2, "pair": 2,
    "three": 3, "four": 4, "five": 5, "six": 6, "dozen": 12,
}
# Quantity units that disambiguate "2 packs" (a count) from "500ml"/"70g"
# (a size). Only a number followed by one of these — or an imperative like
# "add 3" — is treated as a requested quantity.
_QTY_UNIT = (
    r"(?:x|packs?|packets?|pcs?|pieces?|units?|bottles?|cans?|boxes?|"
    r"jars?|tins?|nos?|qty|quantities|quantity)"
)
_QTY_DIGIT_UNIT_RE = re.compile(rf"\b(\d{{1,2}})\s*{_QTY_UNIT}\b", re.IGNORECASE)
_QTY_WORD_UNIT_RE = re.compile(
    rf"\b({'|'.join(_QTY_WORDS)})\b\s+(?:{_QTY_UNIT}|of\b)", re.IGNORECASE
)
_QTY_IMPERATIVE_RE = re.compile(
    r"\b(?:add|buy|get|order|want|need|put)\s+(\d{1,2})\b", re.IGNORECASE
)
_QTY_OF_RE = re.compile(r"\b(\d{1,2})\s+of\b", re.IGNORECASE)
# "qty 4" / "quantity: 3" — the count keyword BEFORE the number.
_QTY_KEYWORD_RE = re.compile(
    r"\b(?:qty|quantity)\s*[:=]?\s*(\d{1,2})\b", re.IGNORECASE
)
# A task that names more than one item — detected by a conjunction/list
# separator. The excess-quantity guard counts add/increment actions across the
# whole task, which only equals one product's quantity for a SINGLE-item task;
# so it disarms itself on multi-item tasks to avoid blocking the 2nd item.
_MULTI_ITEM_RE = re.compile(r"(\band\b|\balso\b|,|;|&|\bplus\b)", re.IGNORECASE)
# Trailing CHECKOUT-ACTION clause, e.g. "...and proceed to checkout" / ", then
# pay". The conjunction here joins an action, NOT a second product, so it must
# be stripped before the multi-item test — otherwise "add milk and proceed to
# checkout" reads as multi-item and wrongly DISARMS the quantity guard (the
# live RMX3392 run where milk over-added because of exactly this). Only the
# unambiguous checkout verbs are listed (proceed/checkout/pay/place order/go to
# cart) so a product named "order"/"review" isn't mistaken for an action.
_TRAILING_ACTION_CLAUSE_RE = re.compile(
    r"[\s,;&]*(?:and\s+|then\s+|&\s+)?"
    r"(?:proceed|checkout|check\s*out|pay|place\s+(?:the\s+)?order|"
    r"go\s+to\s+(?:the\s+)?cart)\b.*$",
    re.IGNORECASE,
)
# Sanity ceiling so a stray big number in the task can't set an absurd target.
_MAX_REQUESTED_QTY = 20
# Category/tile/banner tap intent. Forbidden per prompt rule 1 unless the
# user's task description explicitly invites browsing.
_CATEGORY_INTENT_RE = re.compile(
    r"\b(category|categories|tile|banner|carousel|promo\s+card|"
    r"shop\s+by|browse|explore|collection|landing\s+page)\b",
    re.IGNORECASE,
)
# Task-description keywords that legitimize a category/browse tap. If the
# user said "browse maggi categories" or "explore noodle collections" we
# should NOT reject category taps.
_BROWSE_TASK_RE = re.compile(
    r"\b(browse|explore|category|categories|collection|"
    r"shop\s+by|landing\s+page|see\s+all)\b",
    re.IGNORECASE,
)
# How far vertically from a tap target to look for matching product-name
# text. Grocery cards are 400–600px tall; 250px covers the title/weight/
# price strip but doesn't leak into adjacent products.
_PRODUCT_NEARBY_BAND_PX = 250
# Stop-words to discard when comparing the claimed product name to nearby
# text. Without this filter, a generic note like "tap ADD on the first egg
# product" would compare on "first"/"product" — meaningless. After filter
# only "egg" remains, which must actually appear in nearby text.
_PRODUCT_NOISE_WORDS = frozenset({
    "the", "and", "for", "with", "card", "button", "first", "second",
    "third", "next", "last", "any", "all", "new", "best", "top",
    "product", "item", "row", "tile", "this", "that", "from", "into",
    "qty", "quantity", "pack", "packet", "tap", "add", "cart", "click",
})
# Words that mark a string as price / offer / measurement text rather than a
# product name. On a multi-variant product's options bottom-sheet, each ADD
# button's ancestor content-desc is the OPTION (e.g. "quantity ₹301 rupees,
# offer 20% OFF"), not the product title — so a label made only of these
# tokens is NOT trustworthy ground truth for "which product this ADD buys".
# See _looks_like_product_label / _add_product_name_mismatch.
_PRICE_OFFER_NOISE_WORDS = frozenset({
    "quantity", "qty", "rupee", "rupees", "rs", "inr", "offer", "off",
    "mrp", "price", "save", "discount", "deal", "pack", "pcs", "pc",
    "piece", "pieces", "unit", "units", "each", "per", "inclusive",
    "taxes", "tax", "free", "flat", "upto", "extra",
})
# When the provider reports fewer than this many requests left for the day,
# warn the user so they aren't surprised by a QuotaExceeded mid-task.
LOW_RPD_WARNING_THRESHOLD = 30
# Cap consecutive synthetic waits emitted by dedup. After this many in a row,
# fall through to a real provider call so a frozen UI eventually gets noticed.
MAX_CONSECUTIVE_SYNTHETIC_WAITS = 3
# Cap TOTAL consecutive waits (model-chosen + synthetic). Loop detection only
# considers state-changing actions, so a model that keeps emitting `wait` on a
# blank/frozen screen (e.g. a screen that never loads, or the device asleep)
# isn't otherwise caught — a live run burned all 30 iterations / 264k tokens
# waiting on a black screen. Fail fast after this many so we don't spend a
# whole session (and quota) waiting on a screen that will never change.
MAX_CONSECUTIVE_WAITS = 6
# How many times per task the orchestrator will press BACK itself to surface
# the cart after an ADD + scroll fail to reveal a [CART] element. Capped at 1:
# results -> back -> home (where the cart lives). More than one risks backing
# out of the app entirely; past the cap the model is nudged to tap the now-
# visible cart instead.
MAX_CART_RECOVER_BACKS = 1
# After this many loop-detected events in a single task we give up — the model
# isn't going to escape on its own. Hard-fail with a clear reason.
MAX_LOOPS_BEFORE_ABORT = 4
# After this many CONSECUTIVE identical REJECTED actions, abort. Loop detection
# only counts EXECUTED actions, so a model that keeps proposing the same tap the
# grounding guard rejects (e.g. coords that don't match any tree element) would
# otherwise burn the entire iteration budget — and a paid LLM call each step —
# making no progress. Fail fast instead.
MAX_CONSECUTIVE_REJECTS = 4
# Outcome-verification: if the screen doesn't change after this many state-
# changing actions in a row, try a back-button recovery, then give up.
UNCHANGED_STREAK_RECOVERY = 2
UNCHANGED_STREAK_FAIL = 3
# If the model emits `need_approval` within this many steps of a loop_hint
# being injected, treat the approval as a giveup and hard-terminate. The
# user wants stuck-loops to fail fast, not pause for human rescue. Genuine
# cart-review approvals happen on clean flows where no loop was detected.
LOOP_TO_GIVEUP_WINDOW = 3
# Retry policy for ADB action execution. Index = retry attempt (0..n).
RETRY_BACKOFF_SECONDS = (0.5, 1.0, 2.0)
# Action types whose effect we verify with a follow-up screencap.
_STATE_CHANGING = frozenset({"tap", "type", "swipe"})


ApprovalCallback = Callable[[Task, dict], Awaitable[None]]
StatusCallback = Callable[[Task, str], Awaitable[None]]
# Maps a foreground package name (or None) → the AppProfile that supplies the
# UI-tree annotation vocabulary and arms/disarms the shopping-flow validators.
# main.py injects agent.profiles.resolve_profile; when omitted (unit tests,
# ad-hoc callers) the orchestrator keeps its pre-generalization behaviour by
# defaulting every screen to COMMERCE.
ProfileResolver = Callable[[Optional[str]], AppProfile]


class OrchestratorError(RuntimeError):
    """Raised internally to short-circuit the loop with a failure reason."""


class Orchestrator:
    """Runs the per-task agent loop defined in CLAUDE.md."""

    def __init__(
        self,
        adb: AdbController,
        hitl: HitlGate,
        audit: AuditLogger,
        vision: VisionProvider,
        session_timeout_seconds: int,
        users: UserStore | None = None,
        skills: SkillRegistry | None = None,
        repo: TaskRepository | None = None,
        enable_vision_hitl: bool = False,
        artifact_dir: Path | None = None,
        profile_resolver: ProfileResolver | None = None,
        event_bus=None,
    ) -> None:
        self._adb = adb
        self._hitl = hitl
        self._audit = audit
        self._vision = vision
        self._timeout = session_timeout_seconds
        self._users = users
        self._skills = skills
        self._repo = repo
        self._enable_vision_hitl = enable_vision_hitl
        # Per-app grounding. With a resolver wired (production), each step
        # resolves the foreground package to GENERIC/COMMERCE/... and gates
        # the shopping validators accordingly. Without one (tests / ad-hoc),
        # `_active_profile` stays COMMERCE so behaviour is unchanged.
        self._profile_resolver = profile_resolver
        # Optional in-process event bus (bot.events.EventBus). When set, each
        # step/state is published to dashboard SSE subscribers — IN ADDITION to
        # the Telegram status path, and un-throttled (the dashboard gets every
        # step). None in tests / when the dashboard is disabled.
        self._event_bus = event_bus
        self._active_profile: AppProfile = COMMERCE
        # Root directory for per-task screenshot+UI-dump+action artifacts.
        # None disables persistence (used by unit tests). When set, every
        # step writes step_<NN>.png, step_<NN>.xml, step_<NN>.json under a
        # per-task subdir. This is how the user post-mortems a failed run.
        self._artifact_dir = Path(artifact_dir) if artifact_dir is not None else None
        self.on_approval_request: Optional[ApprovalCallback] = None
        self.on_status_update: Optional[StatusCallback] = None
        self._tasks: dict[int, Task] = {}
        self._last_step_status_at: float = 0.0
        self._low_rpd_warned: bool = False
        # Per-task scratch state, reset at run_task() entry.
        self._last_phash: str | None = None
        self._consecutive_synthetic_waits: int = 0
        self._consecutive_waits: int = 0
        self._cart_recover_backs: int = 0
        self._unchanged_streak: int = 0
        self._loops_detected: int = 0
        self._last_loop_step: int = -1
        self._current_task_db_id: int | None = None
        self._current_task_artifact_dir: Path | None = None
        # True once this task has successfully fetched at least one non-empty
        # UI tree. We use this to distinguish "device doesn't have
        # uiautomator at all" (tree never available — degrade gracefully)
        # from "transient dump failure" (tree usually available — reject
        # taps until it comes back so the model can't tap blind). Reset
        # per task in run_task.
        self._task_tree_ever_seen: bool = False
        # Count of consecutive steps where the dump came back empty AFTER a
        # real tree had been seen. The stale-tree guard rejects blind taps,
        # but some screens (Blinkit's cart / product-detail with permanent
        # carousel animation) NEVER return to idle, so the dump never
        # recovers. If we kept rejecting forever the task would loop until
        # hard-terminate ("socket closed / timeout"). After
        # MAX_STALE_TREE_REJECTS consecutive empties we stop rejecting and
        # let the model act on the screenshot alone — a possibly-imperfect
        # tap beats a guaranteed dead loop. Reset whenever a tree is seen.
        self._consecutive_stale_tree: int = 0
        # Read-only "probe" mode (cross-app comparison). When True the loop
        # rejects add-to-cart / +/- / checkout / pay taps, disarms the
        # cart-seeking guards (auto-back, cart-reveal hint, giveup, premature
        # cart-review), and injects the read-only PROBE_ADDENDUM instead of the
        # COMMERCE shopping rules. Set per-run by run_task(read_only=...);
        # defaults False so ordinary order runs are byte-identical.
        self._read_only: bool = False
        # Count of "finish properly" nudges issued to a read-only probe.
        self._probe_nudges: int = 0
        # Bounded wait for HITL approval (seconds), set per-run by run_task for
        # unattended scheduled tasks. None = wait indefinitely (interactive
        # default — byte-identical to before). On timeout, wait_for_approval
        # returns False and the task fails safely (nothing paid unattended).
        self._approval_timeout: float | None = None

    def get_task(self, user_id: int) -> Task | None:
        return self._tasks.get(user_id)

    async def run_task(
        self,
        task: Task,
        *,
        launch_package: str | None = None,
        read_only: bool = False,
        approval_timeout: float | None = None,
    ) -> Task:
        self._tasks[task.user_id] = task
        self._last_step_status_at = 0.0
        self._low_rpd_warned = False
        self._last_phash = None
        self._consecutive_synthetic_waits = 0
        self._consecutive_waits = 0
        self._cart_recover_backs = 0
        self._unchanged_streak = 0
        self._loops_detected = 0
        self._last_loop_step = -1
        self._current_task_db_id = None
        self._current_task_artifact_dir = self._make_task_artifact_dir(task)
        # Reset to the default; the first real step resolves it from the
        # foreground package. (Stays COMMERCE if no resolver was injected.)
        self._active_profile = COMMERCE
        self._task_tree_ever_seen = False
        self._consecutive_stale_tree = 0
        self._read_only = read_only
        self._approval_timeout = approval_timeout
        self._probe_nudges = 0
        # Attribution + replay metadata for the dashboard (persisted at insert).
        task.launch_package = launch_package
        task.artifact_dir = (
            str(self._current_task_artifact_dir)
            if self._current_task_artifact_dir is not None
            else None
        )
        task.state = TaskState.RUNNING
        await self._persist_insert(task)
        await self._status(task, f"starting: {task.description}")
        self._emit_state(task)
        # Wake the screen before anything else. A sleeping display returns
        # all-black screenshots, on which the model can only ever emit "wait"
        # — a live run wasted its whole session that way after the device dozed
        # off mid-session. Best-effort: a wake failure must never block a task.
        try:
            await self._adb.wake_screen()
        except Exception:
            pass
        if self._current_task_artifact_dir is not None:
            await self._status(
                task,
                f"📸 screenshots → {self._current_task_artifact_dir}",
            )
        # Switch the device's IME to ADBKeyboard for the duration of the task,
        # so the agent's type actions land reliably. Restore the user's normal
        # IME on exit (finally:). Returns None if ADBKeyboard isn't enabled,
        # in which case typing falls back to `adb shell input text`.
        original_ime: str | None = None
        try:
            original_ime = await self._adb.use_adbkeyboard_for_task()
        except Exception:
            original_ime = None
        try:
            if launch_package is not None:
                try:
                    launched = await self._adb.launch_package(launch_package)
                    await self._status(task, f"launched {launched}")
                except AdbError as e:
                    # Don't fail the task — the agent may still be able to
                    # find the app from the home screen. But warn the user.
                    await self._status(
                        task, f"⚠️ couldn't launch {launch_package}: {e}"
                    )
            await asyncio.wait_for(self._loop(task), timeout=self._timeout)
        except asyncio.TimeoutError:
            task.state = TaskState.TIMED_OUT
            task.failure_reason = f"task exceeded {self._timeout}s session timeout"
            _log.warning("task %d %s", task.user_id, task.failure_reason)
            await self._status(task, task.failure_reason)
        except OrchestratorError as e:
            task.state = TaskState.FAILED
            task.failure_reason = str(e)
            _log.warning("task %d failed: %s", task.user_id, e)
            await self._status(task, f"failed: {e}")
        except asyncio.CancelledError:
            task.state = TaskState.FAILED
            task.failure_reason = "cancelled"
            await self._status(task, "cancelled")
            await self._persist_update(task)
            raise
        except Exception as e:
            task.state = TaskState.FAILED
            task.failure_reason = f"unexpected error: {e}"
            # exc_info gives us the full traceback in stdout — previous
            # behaviour was a one-line "unexpected error" in Telegram only.
            _log.exception("task %d unexpected error", task.user_id)
            await self._status(task, task.failure_reason)
        finally:
            # Restore the user's original IME if we swapped for the task.
            # Best-effort — a stuck IME never blocks task completion.
            try:
                await self._adb.restore_ime(original_ime)
            except Exception:
                pass
        await self._persist_update(task)
        self._emit_state(task)
        return task

    async def _loop(self, task: Task) -> None:
        screen_size = await self._safe_screen_size()

        max_iters = MAX_PROBE_ITERATIONS if self._read_only else MAX_LOOP_ITERATIONS
        for _ in range(max_iters):
            task.step_count += 1
            _log.info("step %d: begin", task.step_count)

            # Repeated-rejection breaker: if the model keeps proposing the same
            # action the guards reject (it never executes), it makes no progress
            # and wastes an LLM call per step. Abort before screencap/vision so
            # we don't burn the rest of the budget on a guaranteed-rejected tap.
            if _repeated_rejection_count(task.history) >= MAX_CONSECUTIVE_REJECTS:
                raise OrchestratorError(
                    "stuck: the model repeated the same rejected action "
                    f"{MAX_CONSECUTIVE_REJECTS}x (its coords/intent don't match "
                    "any on-screen element). Aborting instead of burning the budget."
                )

            _log.info("step %d: screencap", task.step_count)
            screenshot = await self._adb.screencap()
            screenshot_b64 = base64.standard_b64encode(screenshot).decode("ascii")
            current_phash = await _safe_phash(screenshot)

            # The UI tree is fetched once per iteration. We use it for two
            # purposes: (1) inject into the model prompt for accurate
            # coordinates, (2) validate the model's tap/swipe coords against
            # known element bounds to reject hallucinated coords. Both
            # branches below need access to `ui_elements` for the validation.
            ui_tree: str | None = None
            ui_elements: list[UiElement] = []

            # Dedup: if the screen is visually identical to what the model
            # last saw and we already took at least one action, skip the
            # provider call and synthesize a wait. The model has nothing new
            # to react to.
            synthetic = self._maybe_synthesize_wait(task, current_phash)
            if synthetic is not None:
                action = synthetic
                self._consecutive_synthetic_waits += 1
                # Synthetic wait path doesn't fetch a fresh ui tree, but we
                # still want a screenshot artifact for this step so the
                # post-mortem timeline isn't missing frames.
                self._save_step_artifacts(
                    task.step_count, screenshot, None, action, "synthetic-wait"
                )
            else:
                self._consecutive_synthetic_waits = 0
                _log.info("step %d: skill+ui_tree", task.step_count)
                # One foreground-package read per step, shared by skill lookup
                # AND profile resolution (was a separate dumpsys per concern).
                pkg = await self._foreground_package()
                self._active_profile = self._resolve_profile(pkg)
                skill_hint = self._compose_guidance(self._active_profile, pkg)
                ui_tree, ui_elements, ui_xml = await self._lookup_ui_tree(
                    self._active_profile
                )
                if ui_elements:
                    # Latch: once we've seen a real tree on this task, any
                    # later empty result is a transient failure (mid-app
                    # animation) rather than a "this device strips
                    # uiautomator" situation. The rejection check below uses
                    # this to refuse blind taps.
                    self._task_tree_ever_seen = True
                    self._consecutive_stale_tree = 0
                # Auto-recover the cart: after an ADD, if the model has already
                # scrolled and there's STILL no [CART] element, press BACK
                # ourselves to return to the app home (where the cart bar
                # lives) — instead of relying on the model, which otherwise
                # stalls or escapes via need_approval ("should I go back?").
                # Bounded by MAX_CART_RECOVER_BACKS so we never back out of the
                # app. Deterministic action, no provider call this step.
                if (
                    self._active_profile.enforce_shopping_guards
                    and not self._read_only
                    and self._cart_recover_backs < MAX_CART_RECOVER_BACKS
                    and self._should_auto_back_to_cart(task, ui_elements)
                ):
                    self._cart_recover_backs += 1
                    await self._auto_back_for_cart(task, screenshot, ui_xml)
                    self._last_phash = None
                    continue
                loop_hint = self._loop_hint(task)
                if loop_hint:
                    self._audit.log_action(
                        task.user_id, task.description, {"action": "loop_hint"},
                        f"INJECTED: {loop_hint}",
                    )
                    if self._loops_detected >= MAX_LOOPS_BEFORE_ABORT:
                        raise OrchestratorError(
                            f"stuck: detected {self._loops_detected} action loops; "
                            "the model isn't escaping. Aborting."
                        )
                # Post-ADD cart-reveal hint: an item is in the cart but no
                # [CART] element is in the tree (the floating View-cart bar
                # isn't surfaced on the results screen until you scroll/back).
                # Steer the model to reveal it instead of floundering. Commerce
                # apps only.
                cart_hint = (
                    self._cart_reveal_hint(task, ui_elements)
                    if self._active_profile.enforce_shopping_guards
                    and not self._read_only else ""
                )
                if cart_hint:
                    self._audit.log_action(
                        task.user_id, task.description, {"action": "cart_reveal_hint"},
                        f"INJECTED: {cart_hint}",
                    )
                injected = "\n\n".join(h for h in (loop_hint, cart_hint) if h)
                _log.info("step %d: vision call", task.step_count)
                response = await self._vision.get_next_action(
                    screenshot_bytes=screenshot,
                    task_description=(
                        f"{task.description}\n\n{injected}" if injected else task.description
                    ),
                    step_history=task.history[-PROMPT_HISTORY_WINDOW:],
                    screen_size=screen_size,
                    skill_hint=skill_hint,
                    ui_tree=ui_tree,
                )
                action = response.action
                _log.info(
                    "step %d: vision returned %s",
                    task.step_count, action.get("action"),
                )
                self._record_usage(task, response.usage)
                await self._maybe_warn_low_rpd(task)
                # `_last_phash` records the screen the MODEL last saw. Update
                # here (after a real call) and nowhere else — the dedup check
                # depends on this invariant.
                self._last_phash = current_phash
                # Persist the model-input artifacts (screenshot, raw XML,
                # parsed UI prompt block) BEFORE we decide whether the action
                # is acceptable. That way the post-mortem shows exactly what
                # the model saw and what it proposed, even if we then reject
                # it downstream. The .json update at the end of execute
                # records the final result so you can correlate.
                self._save_step_artifacts(
                    task.step_count, screenshot, ui_xml, action,
                    "(pending)", ui_prompt=ui_tree,
                )
                # Stream the intended action + its screenshot to the dashboard
                # BEFORE we decide to execute/reject it (the file is on disk, so
                # the screenshot URL is already valid). Un-throttled.
                self._emit_step(task, action, "(pending)", phase="pending")

            # Consecutive-wait cap: a model that keeps waiting on a screen that
            # never changes (frozen app, or a sleeping/blank display) makes no
            # progress and burns a provider call every iteration. Loop
            # detection ignores `wait` (it only tracks tap/type/swipe), so cap
            # it here and fail fast with an actionable reason.
            if action.get("action") == "wait":
                self._consecutive_waits += 1
                if self._consecutive_waits >= MAX_CONSECUTIVE_WAITS:
                    raise OrchestratorError(
                        f"screen did not change after {self._consecutive_waits} "
                        "consecutive waits — the app is frozen or the screen is "
                        "blank (is the device awake and unlocked?). Aborting "
                        "instead of waiting out the session."
                    )
            else:
                self._consecutive_waits = 0

            ok, reason = validate(action)
            if not ok:
                self._audit.log_action(
                    task.user_id, task.description, action, f"BLOCKED: {reason}"
                )
                raise OrchestratorError(f"action validation failed: {reason}")

            # Stale-tree rejection: this task has previously fetched a real
            # UI tree, but now the dump returned nothing. That's a transient
            # uiautomator failure (typically: mid-animation, autocomplete
            # dropdown opening, app transition). Without a tree, none of our
            # coord/intent/product-name validators can fire — the model
            # would tap completely blind. Reject taps and swipes until the
            # tree recovers; wait/done/need_approval still pass.
            if (
                self._task_tree_ever_seen
                and not ui_elements
                and action.get("action") in {"tap", "swipe"}
            ):
                self._consecutive_stale_tree += 1
                if self._consecutive_stale_tree <= MAX_STALE_TREE_REJECTS:
                    hint = (
                        "REJECTED: UI tree dump returned no elements this step "
                        "(transient uiautomator failure during animation). "
                        "Don't tap blind. Emit a wait action so the screen can "
                        "settle, then re-evaluate."
                    )
                    task.history.append({"action": action, "result": hint})
                    self._audit.log_action(
                        task.user_id, task.description, action,
                        "STALE_TREE_REJECTED",
                    )
                    await self._step_status(
                        task,
                        f"step {task.step_count}: UI dump empty, asking for wait",
                    )
                    self._finalize_step_result(task.step_count, action, hint)
                    self._last_phash = None
                    continue
                # Dump has been stuck for MAX_STALE_TREE_REJECTS+1 consecutive
                # steps — this screen never reaches idle (e.g. Blinkit's cart
                # with a permanent carousel). Stop rejecting: a screenshot-
                # grounded tap is far better than looping until the session
                # dies with a socket-closed/timeout error. Fall through to
                # execute. Coord/intent/product checks below also no-op on an
                # empty tree, so the action runs as the model intended.
                self._audit.log_action(
                    task.user_id, task.description, action,
                    f"STALE_TREE_OVERRIDE: dump empty "
                    f"{self._consecutive_stale_tree} steps running; allowing "
                    "screenshot-grounded action through",
                )
                await self._step_status(
                    task,
                    f"step {task.step_count}: UI dump still empty after "
                    f"{self._consecutive_stale_tree} tries — proceeding on "
                    "screenshot",
                )

            # Coord grounding: tap/swipe coords must map to some element in
            # the UI tree (with a 20px forgiveness margin). If they don't,
            # the model is hallucinating "tap on X" at coords that don't
            # actually point to X. Reject the action, hint the model, retry
            # next iteration.
            mismatch = self._coords_mismatch(action, ui_elements)
            if mismatch is not None:
                hint = (
                    f"REJECTED: {mismatch}. Pick coords from the UI elements "
                    "listing — never invent or estimate coordinates."
                )
                task.history.append({"action": action, "result": hint})
                self._audit.log_action(
                    task.user_id, task.description, action, "COORD_REJECTED"
                )
                await self._step_status(
                    task, f"step {task.step_count}: coords rejected, retrying"
                )
                self._finalize_step_result(task.step_count, action, hint)
                # Don't let dedup short-circuit the next iteration — we need a
                # fresh model call so it sees the rejection and adjusts.
                self._last_phash = None
                continue

            # Intent-vs-element check: the coord lands SOMEWHERE in the tree
            # (passed coord grounding) but on the WRONG kind of element for
            # what the note says. Catches the search-bar-vs-location-header
            # misclick and ADD-on-non-action hallucinations.
            intent_problem = (
                self._intent_mismatch(action, ui_elements)
                if self._active_profile.enforce_shopping_guards else None
            )
            if intent_problem is not None:
                hint = (
                    f"REJECTED: {intent_problem}. Pick a different element "
                    "whose type matches your stated intent."
                )
                task.history.append({"action": action, "result": hint})
                self._audit.log_action(
                    task.user_id, task.description, action,
                    "INTENT_MISMATCH_REJECTED",
                )
                await self._step_status(
                    task,
                    f"step {task.step_count}: intent/element mismatch, retrying",
                )
                self._finalize_step_result(task.step_count, action, hint)
                self._last_phash = None
                continue

            # Read-only probe rejection (cross-app comparison): this run is a
            # price check, so any tap on an ADD / +/- / Buy / Checkout / Pay
            # button is forbidden — it would mutate the cart. Reject before
            # execution so the model reads the price and finishes with a
            # `report` action instead. Only active in read_only mode.
            readonly_problem = (
                self._readonly_violation_rejection(action, ui_elements)
                if self._read_only else None
            )
            if readonly_problem is not None:
                hint = f"REJECTED: {readonly_problem}"
                task.history.append({"action": action, "result": hint})
                self._audit.log_action(
                    task.user_id, task.description, action,
                    "READONLY_VIOLATION_REJECTED",
                )
                await self._step_status(
                    task,
                    f"step {task.step_count}: read-only probe — cart taps "
                    "blocked, reading price only",
                )
                self._finalize_step_result(task.step_count, action, hint)
                self._last_phash = None
                continue

            # Read-only probe completeness: a price probe must end with a
            # `report` that reflects an actually-opened result — not a bare
            # `done` (silent bail), and not a price=null report while still on
            # the search/results screen (food cards show ETA, not price — the
            # model must OPEN a result to read the price). Bounded by
            # MAX_PROBE_NUDGES so it can never loop the probe.
            if self._read_only and self._probe_nudges < MAX_PROBE_NUDGES:
                probe_hint = self._readonly_probe_incomplete(task, action)
                if probe_hint is not None:
                    self._probe_nudges += 1
                    hint = f"REJECTED: {probe_hint}"
                    task.history.append({"action": action, "result": hint})
                    self._audit.log_action(
                        task.user_id, task.description, action,
                        "READONLY_PROBE_INCOMPLETE",
                    )
                    await self._step_status(
                        task,
                        f"step {task.step_count}: open the result and read the "
                        "price before reporting",
                    )
                    self._finalize_step_result(task.step_count, action, hint)
                    self._last_phash = None
                    continue

            # Product-name vs tap-coords check: the model claimed
            # "tap ADD on <product>" but the UI elements near the tap
            # coords don't mention <product> at all. Real failure case:
            # claimed eggs, actually added a Fire TV Stick. Reject so the
            # model has to re-target.
            name_problem = (
                self._add_product_name_mismatch(action, ui_elements)
                if self._active_profile.enforce_shopping_guards else None
            )
            if name_problem is not None:
                hint = f"REJECTED: {name_problem}."
                task.history.append({"action": action, "result": hint})
                self._audit.log_action(
                    task.user_id, task.description, action,
                    "PRODUCT_NAME_MISMATCH_REJECTED",
                )
                await self._step_status(
                    task,
                    f"step {task.step_count}: product name doesn't match coords",
                )
                self._finalize_step_result(task.step_count, action, hint)
                self._last_phash = None
                continue

            # Category/tile/banner tap rejection: the model is explicitly
            # violating prompt rule 1 (forbidden navigation for add-to-cart
            # flows). Block early so the model has to find a real product
            # card instead of getting lost on a landing page.
            category_problem = (
                self._category_tap_rejection(task, action)
                if self._active_profile.enforce_shopping_guards else None
            )
            if category_problem is not None:
                hint = f"REJECTED: {category_problem}."
                task.history.append({"action": action, "result": hint})
                self._audit.log_action(
                    task.user_id, task.description, action,
                    "CATEGORY_TAP_REJECTED",
                )
                await self._step_status(
                    task,
                    f"step {task.step_count}: category tap blocked, retrying",
                )
                self._finalize_step_result(task.step_count, action, hint)
                self._last_phash = None
                continue

            # Same-coords ADD-after-ADD (any product name) is wrong — see
            # _repeated_add_rejection docstring. Reject before execution so
            # the loop detector doesn't consume a slot on it.
            repeat_problem = (
                self._repeated_add_rejection(task, action)
                if self._active_profile.enforce_shopping_guards else None
            )
            if repeat_problem is not None:
                hint = f"REJECTED: {repeat_problem}."
                task.history.append({"action": action, "result": hint})
                self._audit.log_action(
                    task.user_id, task.description, action,
                    "REPEATED_ADD_REJECTED",
                )
                await self._step_status(
                    task,
                    f"step {task.step_count}: repeated-ADD hallucination, retrying",
                )
                self._finalize_step_result(task.step_count, action, hint)
                self._last_phash = None
                continue

            # Excess-quantity rejection: the requested number of units is
            # already in the cart; another ADD/+ would over-add (the live
            # "3 milks instead of 1" finding). Reject so the model goes to
            # the cart instead of bumping the stepper again.
            excess_problem = (
                self._excess_quantity_rejection(task, action)
                if self._active_profile.enforce_shopping_guards else None
            )
            if excess_problem is not None:
                hint = f"REJECTED: {excess_problem}"
                task.history.append({"action": action, "result": hint})
                self._audit.log_action(
                    task.user_id, task.description, action,
                    "EXCESS_QUANTITY_REJECTED",
                )
                await self._step_status(
                    task,
                    f"step {task.step_count}: quantity already met, go to cart",
                )
                self._finalize_step_result(task.step_count, action, hint)
                self._last_phash = None
                continue

            # Type-without-focused-input rejection: ADB typing only lands
            # in a focused EditText. If the model emits `type` without
            # having focused an input, the characters go nowhere — and
            # the model then hallucinates over what should be a search-
            # results page that never loaded. Catch it before execution.
            type_focus_problem = self._type_without_focus_rejection(
                action, ui_elements
            )
            if type_focus_problem is not None:
                hint = f"REJECTED: {type_focus_problem}."
                task.history.append({"action": action, "result": hint})
                self._audit.log_action(
                    task.user_id, task.description, action,
                    "TYPE_WITHOUT_FOCUS_REJECTED",
                )
                await self._step_status(
                    task,
                    f"step {task.step_count}: no focused input — tap "
                    "EditText first, then type",
                )
                self._finalize_step_result(task.step_count, action, hint)
                self._last_phash = None
                continue

            # Premature-cart-review rejection: model emits a "Cart review"
            # need_approval while still on the search-results page (with
            # hallucinated cart items pulled from cross-sell [ACTION] lines).
            # Reject before HITL so the cart-screen handoff doesn't auto-
            # grant a payment latch on a stale screen.
            premature_reason = (
                self._premature_cart_review_rejection(action, ui_elements)
                if self._active_profile.enforce_shopping_guards
                and not self._read_only else None
            )
            if premature_reason is not None:
                hint = f"REJECTED need_approval: {premature_reason}"
                task.history.append({"action": action, "result": hint})
                self._audit.log_action(
                    task.user_id, task.description, action,
                    "REJECTED: premature cart-review",
                )
                await self._step_status(
                    task,
                    f"step {task.step_count}: premature cart-review — "
                    "navigate to cart first",
                )
                self._finalize_step_result(task.step_count, action, hint)
                self._last_phash = None
                continue

            # Giveup rejection: model emits need_approval with a "no products
            # found"-style reason before having actually scrolled, OR while
            # contradicting a recent ADD tap. Don't pause for the user —
            # force a recovery attempt. This mirrors the coord-rejection
            # shape: append a hint to history, reset dedup, continue.
            # Legitimate need_approval (cart review, payment, no payment
            # method, etc.) doesn't trip this.
            giveup_reason = (
                self._giveup_rejection(task, action)
                if self._active_profile.enforce_shopping_guards
                and not self._read_only else None
            )
            if giveup_reason is not None:
                hint = (
                    f"REJECTED need_approval: {giveup_reason}. The "
                    "orchestrator no longer trusts textual claims like "
                    "'after scrolling' — only an actual executed swipe in "
                    "history counts as proof of scrolling. Required next "
                    "action: emit a real swipe (e.g. swipe from (540,1600) "
                    "to (540,800)) to scroll the results. If you just "
                    "claimed to tap ADD on a product, navigate to the cart "
                    "icon instead of emitting need_approval — the ADD "
                    "either landed (go to cart) or missed (retry the tap)."
                )
                task.history.append({"action": action, "result": hint})
                self._audit.log_action(
                    task.user_id, task.description, action,
                    "REJECTED: giveup before scrolling",
                )
                await self._step_status(
                    task,
                    f"step {task.step_count}: need_approval rejected — "
                    "scroll first",
                )
                self._finalize_step_result(task.step_count, action, hint)
                self._last_phash = None
                continue

            # Benign-navigation need_approval: the model asking permission to
            # press back / go home / tap the cart, rather than a real sensitive
            # handoff. Reject so it just navigates instead of stalling the user.
            benign_reason = (
                self._benign_approval_rejection(action)
                if self._active_profile.enforce_shopping_guards else None
            )
            if benign_reason is not None:
                hint = f"REJECTED need_approval: {benign_reason}"
                task.history.append({"action": action, "result": hint})
                self._audit.log_action(
                    task.user_id, task.description, action,
                    "REJECTED: need_approval for benign navigation",
                )
                await self._step_status(
                    task,
                    f"step {task.step_count}: need_approval for navigation rejected "
                    "— just navigate",
                )
                self._finalize_step_result(task.step_count, action, hint)
                self._last_phash = None
                continue

            _log.info("step %d: hitl gate", task.step_count)
            await self._gate_with_hitl(task, action, screenshot, screenshot_b64, current_phash)

            # Security rule 4: audit BEFORE execute, never after.
            self._audit.log_action(
                task.user_id, task.description, action, "EXECUTING"
            )
            _log.info("step %d: execute %s", task.step_count, action.get("action"))

            action_type = action.get("action")
            if action_type == "done":
                task.state = TaskState.DONE
                task.final_summary = action.get("summary", "")
                task.history.append({"action": action, "result": "done"})
                await self._persist_step(task, action, "done")
                self._finalize_step_result(task.step_count, action, "done")
                await self._status(task, f"done: {task.final_summary}")
                return

            if action_type == "report":
                # Read-only probe terminal (cross-app comparison). Like `done`,
                # but carries a structured quote in `data` that the comparison
                # engine reads off task.report. Never dispatched to the device.
                data = action.get("data")
                task.report = data if isinstance(data, dict) else {}
                task.state = TaskState.DONE
                task.final_summary = action.get("summary", "") or _summarize_report(
                    task.report
                )
                task.history.append({"action": action, "result": "report"})
                await self._persist_step(task, action, "report")
                self._finalize_step_result(task.step_count, action, "report")
                await self._status(task, f"report: {task.final_summary}")
                return

            result = await self._execute_with_retry(task, action)
            task.history.append({"action": action, "result": result})
            self._audit.log_action(
                task.user_id, task.description, action, f"RESULT: {result}"
            )
            await self._persist_step(task, action, result)
            self._finalize_step_result(task.step_count, action, result)
            self._emit_step(task, action, result, phase="final")
            note = str(action.get("note", "")).strip()
            status_msg = (
                f"step {task.step_count}: {result} — {note}"
                if note
                else f"step {task.step_count}: {result}"
            )
            await self._step_status(task, status_msg)

            # Outcome verification is meaningful only for state-changing
            # actions. A wait (real or synthetic) doesn't reset the streak —
            # a stuck UI stays stuck whether or not we pause between probes.
            if action_type in _STATE_CHANGING:
                await self._verify_outcome(task, action, current_phash)

        raise OrchestratorError(f"exceeded {max_iters} loop iterations")

    # ------------------------------------------------------------------
    # Dedup + skill lookup

    def _maybe_synthesize_wait(self, task: Task, phash: str | None) -> dict | None:
        if phash is None or self._last_phash is None:
            return None
        if task.step_count <= 1:
            return None
        if phash != self._last_phash:
            return None
        if self._consecutive_synthetic_waits >= MAX_CONSECUTIVE_SYNTHETIC_WAITS:
            return None
        # Don't synthesize back-to-back waits if the model just chose to wait —
        # they're indistinguishable to the next iteration and we want the
        # model to see at least one real call between waits.
        last = task.history[-1]["action"] if task.history else {}
        if last.get("action") == "wait":
            return None
        return {"action": "wait", "reason": "screen unchanged"}

    async def _foreground_package(self) -> str | None:
        """Best-effort foreground package; None on any adb hiccup."""
        try:
            return await self._adb.get_foreground_package()
        except Exception:
            return None

    def _compose_guidance(
        self, profile: AppProfile, package: str | None
    ) -> str | None:
        """Per-app guidance block for the prompt: profile addendum + skill md.

        The profile's addendum (e.g. COMMERCE's shopping-flow rules) leads,
        followed by the package-specific `skills/<pkg>.md` text if any. Under
        GENERIC the addendum is empty, so a non-commerce app contributes only
        its own skill (usually none) — the shopping rules never reach it.
        Returns None when there's nothing to add, so the provider omits the
        whole block.
        """
        parts: list[str] = []
        if profile.prompt_addendum:
            # In read-only probe mode (cross-app comparison) the COMMERCE
            # shopping rules — which actively drive toward add-to-cart — are
            # exactly wrong. Swap in the read-only PROBE_ADDENDUM so the model
            # only reads the price and finishes with a `report` action.
            if self._read_only and profile.enforce_shopping_guards:
                parts.append(prompts.PROBE_ADDENDUM)
            else:
                parts.append(profile.prompt_addendum)
        skill = self._skills.get(package) if self._skills else None
        if skill:
            parts.append(skill)
        return "\n\n".join(parts) if parts else None

    def _resolve_profile(self, package: str | None) -> AppProfile:
        """Resolve the active grounding profile for a foreground package.

        With a resolver injected (production), maps the package to
        GENERIC/COMMERCE/... With none (tests / ad-hoc callers), keeps the
        pre-generalization behaviour of treating every screen as COMMERCE.
        """
        if self._profile_resolver is None:
            return COMMERCE
        try:
            return self._profile_resolver(package)
        except Exception:
            return COMMERCE

    async def _lookup_ui_tree(
        self, profile: AppProfile,
    ) -> tuple[str | None, list[UiElement], str | None]:
        """Fetch the on-screen accessibility tree under the active profile.

        Returns a 3-tuple: (rendered prompt block, parsed element list,
        raw XML). The rendered block goes into the model prompt; the parsed
        list is used by the coord-validation check to reject hallucinated
        taps; the raw XML is persisted as a per-step artifact so failed
        runs can be inspected later. All three default to None/[]/None on
        failure so callers can degrade. `profile` supplies the annotation
        vocabulary — GENERIC yields a structural-only tree with no shopping
        tags.
        """
        try:
            xml = await self._adb.dump_ui_xml()
        except Exception:
            return None, [], None
        if not xml:
            return None, [], None
        # Parse the dump XML ONCE and render the prompt block from the same
        # element list. The previous code called parse(xml) here and
        # to_prompt_section(xml) — which parsed the XML a second time — so
        # every step re-ran ElementTree + the full candidate walk twice.
        elements = ui_tree_parse(xml, profile)
        rendered = ui_tree_render(elements, profile) or None
        return rendered, elements, xml

    @staticmethod
    def _cart_reveal_hint(task: Task, elements: list[UiElement]) -> str:
        """Proactive hint when an item is added but no cart path is visible.

        Production finding (2026-06-08 live Blinkit run): after a successful
        ADD, the floating "View cart" bar is NOT in the accessibility tree on
        the search-results screen — verified zero cart nodes across the whole
        run. With no [CART] element to tap (and coord-grounding rejecting taps
        that don't hit a tree node), the model floundered: it gave up, fiddled
        the stepper, and added more products. The bar only enters the tree
        after you scroll, or after pressing back to the app home.

        So once an ADD has landed and there's STILL no cart element in the
        tree — and we're not already on a cart/checkout screen — nudge the
        model to reveal the bar instead of acting on the product again. This
        pairs with the orchestrator's auto-back (`_should_auto_back_to_cart`):
        swipe up FIRST (cheap); once a scroll has been tried the orchestrator
        presses back to home itself, and from there this hint tells the model
        to TAP the now-visible cart bar/icon. We deliberately never tell the
        model to press back — the orchestrator owns that, and a back-press
        from the home screen would exit the app. Returns "" when no nudge is
        warranted. Caller gates on the commerce profile.
        """
        if not _history_has_executed_add(task.history):
            return ""
        # A cart path is already visible — nothing to reveal.
        if any(e.is_cart_bar for e in elements):
            return ""
        # Already on a cart/checkout screen — the bar's job is done.
        haystack = " ".join((e.text + " " + e.desc).lower() for e in elements)
        if any(tok in haystack for tok in _CART_SCREEN_TOKENS):
            return ""
        if _swipe_after_last_add(task.history):
            # Scrolled already; the orchestrator has/will press back to home.
            # The cart bar/icon should be on screen now even if it isn't a
            # detected [CART] node — tell the model to tap it visually.
            return (
                "NOTE: the item is already in your cart. A 'View cart' bar / cart "
                "icon should now be visible on screen (a bar along the bottom, or "
                "a cart icon at the top-right) even though it isn't tagged [CART] "
                "in the list. TAP it at its on-screen position to open the cart. "
                "Do NOT press back again (you would leave the app), and do NOT "
                "tap ADD or the +/- stepper — the item is already added."
            )
        return (
            "NOTE: the item is already in your cart, but there is no 'View cart' "
            "/ [CART] element in the UI list — on this app the floating cart bar "
            "often isn't surfaced on the results screen until you scroll. SWIPE "
            "UP (start the swipe on a product card, e.g. drag from the lower "
            "third of the screen toward the top) to reveal the cart bar, then "
            "tap it. Do NOT tap ADD or the +/- stepper again; the item is "
            "already added."
        )

    @staticmethod
    def _should_auto_back_to_cart(task: Task, elements: list[UiElement]) -> bool:
        """True when the orchestrator should press BACK to surface the cart.

        Fires when an item is in the cart, the model has ALREADY swiped since
        the ADD (the cheap reveal was tried), there's still no [CART] element,
        and we're not already on a cart/checkout screen. The caller also
        enforces the commerce profile and the MAX_CART_RECOVER_BACKS cap, so
        we only ever back out once (results -> home).
        """
        if not _history_has_executed_add(task.history):
            return False
        if not _swipe_after_last_add(task.history):
            return False
        if any(e.is_cart_bar for e in elements):
            return False
        haystack = " ".join((e.text + " " + e.desc).lower() for e in elements)
        if any(tok in haystack for tok in _CART_SCREEN_TOKENS):
            return False
        return True

    async def _auto_back_for_cart(
        self, task: Task, screenshot: bytes, ui_xml: str | None
    ) -> None:
        """Press BACK to return toward the app home so the cart bar appears.

        Best-effort — a flaky back-press must not kill the task. Records a
        `recover` history entry (ignored by the tap-based loop/giveup
        scanners) so the next model call sees what the orchestrator did.
        """
        action = {
            "action": "recover",
            "note": "auto: pressed back to surface the cart bar (none found after scroll)",
        }
        try:
            await self._adb.key_event(KEYCODE_BACK)
        except AdbError as e:
            self._audit.log_action(
                task.user_id, task.description, action, f"AUTO_RECOVER_FAILED: {e}"
            )
            return
        result = "auto-recover: pressed back toward home to reveal the cart"
        task.history.append({"action": action, "result": result})
        self._audit.log_action(
            task.user_id, task.description, action, "AUTO_RECOVER: back-to-home for cart"
        )
        await self._step_status(
            task,
            f"step {task.step_count}: no cart bar after scroll — pressed back to find it",
        )
        self._save_step_artifacts(task.step_count, screenshot, ui_xml, action, result)

    @staticmethod
    def _benign_approval_rejection(action: dict) -> str | None:
        """Reject a need_approval that's really asking permission for a benign
        navigation, not a genuine sensitive handoff.

        Production finding: with no [CART] element the model emitted
        need_approval "...should I proceed with going back to the home
        screen?" and stalled the user. need_approval is reserved (rule 10) for
        payment / OTP / delete / permission / cart-review. We reject a
        navigation-ask — UNLESS the reason also names a genuinely sensitive
        category (then it reaches the user untouched).
        """
        if action.get("action") != "need_approval":
            return None
        reason = str(action.get("reason", ""))
        if not reason or _LEGITIMATE_SENSITIVE_RE.search(reason):
            return None
        if not _NAV_ESCAPE_RE.search(reason):
            return None
        return (
            "need_approval is only for payment / OTP / delete / permission / "
            "cart-review — NOT for navigation. You do not need permission to "
            "press back, swipe, scroll, or tap the cart; just do it. If the item "
            "is added and no [CART] element is listed, swipe up to reveal the "
            "cart bar (the orchestrator will return you toward the home screen if "
            "that fails), then tap the visible cart bar/icon"
        )

    @staticmethod
    def _readonly_violation_rejection(
        action: dict, elements: list[UiElement]
    ) -> str | None:
        """Reject a cart-mutating tap during a read-only price probe.

        A probe (cross-app comparison) may search, open a product, scroll, and
        read the price — but it must NOT add to cart. Two signals, either is
        enough: (1) the element under the tap coords is a primary [ACTION]
        button (ADD / +/- / Buy / Checkout / Pay — `find_action_at`), or
        (2) the model's own `note` states an add/checkout/pay intent
        (`_ADD_INTENT_RE`). Returns a hint steering the model to `report`
        instead, or None when the tap is a benign navigation/read.
        """
        if action.get("action") != "tap":
            return None
        note = str(action.get("note", ""))
        target = None
        try:
            x = int(action.get("x"))
            y = int(action.get("y"))
            target = find_action_at(x, y, elements)
        except (TypeError, ValueError):
            target = None
        if target is None and not _READONLY_FORBIDDEN_NOTE_RE.search(note):
            return None
        return (
            "this is a READ-ONLY price check — do NOT tap ADD / +/- / Buy / "
            "Checkout / Pay / Place Order / Book / Confirm / Request (those "
            "commit an order or booking). Just read "
            "the displayed price and delivery time. Open the product if you "
            "need the price, then finish with a report action: "
            '{"action":"report","data":{"price":<number or null>,'
            '"currency":"INR","eta":"<string or null>","available":true,'
            '"item_name":"<what you found>","notes":"<short>"}}'
        )

    @staticmethod
    def _readonly_probe_incomplete(task: Task, action: dict) -> str | None:
        """Return a nudge if a read-only probe is trying to finish without a
        real price, else None.

        Two cases:
          1. `done` — a probe must finish with a `report`, never a bare done
             (the Swiggy bail: searched, then `done`, zero data).
          2. `report` with price=null while the model has NOT opened a result
             since searching (no executed tap after the last `type`). Food
             results show ETA, not price; the model must open a dish/restaurant
             to read the price (the Zomato bail: reported null from results).

        A price=null report AFTER opening a result is accepted (genuine "no
        price"), as is available=false (unavailable).
        """
        atype = action.get("action")
        if atype == "done":
            return (
                "this is a price check — do NOT finish with `done`. Read the "
                "price and emit a report action. If you haven't opened a result "
                "yet, tap the most relevant one first to see its price."
            )
        if atype == "report":
            data = action.get("data")
            data = data if isinstance(data, dict) else {}
            if data.get("price") is None and data.get("available") is not False:
                if _taps_since_last_type(task.history) == 0:
                    return (
                        "you reported no price but you're still on the search/"
                        "results screen — cards here often show only a delivery "
                        "time, not the price. TAP the most relevant result to "
                        "open it, read the item's price, then report. Report "
                        "price=null only if there's truly no price after opening."
                    )
        return None

    @staticmethod
    def _giveup_rejection(task: Task, action: dict) -> str | None:
        """Return a rejection reason if `action` is a premature "no products
        found"-style bail-out, else None.

        Two independent rejection paths:

        1. Giveup-before-scroll: the reason matches a `_GIVEUP_PATTERNS` regex
           AND task.history shows no executed swipe. The only valid bypass is
           an actual swipe in history — earlier versions accepted a textual
           "after scrolling" suffix as proof, but the model learned to fake
           that suffix, so it's been removed.

        2. Giveup-contradicting-ADD: the most recent executed action is a tap
           whose note matches `_ADD_TAP_NOTE_RE` (e.g. "tap ADD on Maggi …"),
           and yet the model immediately emits a "no products found" giveup.
           Those two statements can't both be true — either the tap actually
           added a product (then the next step is cart-review, not giveup) or
           the tap missed (then the model should retry, not bail). Block.

        Genuine sensitive HITL (cart review, payment, no payment method, OTP)
        doesn't match any of these patterns, so it passes through untouched.
        """
        if action.get("action") != "need_approval":
            return None
        reason = str(action.get("reason", ""))
        if not reason:
            return None
        match = None
        for p in _GIVEUP_PATTERNS:
            m = p.search(reason)
            if m is not None:
                match = m
                break
        if match is None:
            return None

        # Contradiction check: the model's last executed action was an apparent
        # ADD tap. The giveup can't be honest — reject regardless of whether
        # scrolling happened. Search through history for the LAST executed
        # tap; skip entries that were rejected at the orchestrator level.
        last_tap = _last_executed_tap(task.history)
        if last_tap is not None:
            note = str(last_tap.get("note", ""))
            if _ADD_TAP_NOTE_RE.search(note):
                return (
                    f"reason matches giveup pattern '{match.group(0)}' but "
                    f"the previous tap was '{note}' — these are contradictory. "
                    "Either the ADD landed (then the cart is the next stop) or "
                    "it missed (then retry, don't bail)"
                )

        if _history_has_swipe(task.history):
            return None
        return (
            f"reason matches giveup pattern '{match.group(0)}' but no "
            "scroll has been attempted"
        )

    @staticmethod
    def _premature_cart_review_rejection(
        action: dict, elements: list[UiElement]
    ) -> str | None:
        """Reject `need_approval` with a "Cart review" reason when the
        current screen clearly isn't the cart screen.

        Failure mode from production runs: after a successful ADD on the
        search results page, the model emits

            {"action":"need_approval","reason":"Cart review: <items>"}

        while still on the search results, hallucinating cart contents from
        the [ACTION] product lines of cross-sell rows. Approving this would
        auto-grant the payment latch and let the model fly past the actual
        cart review.

        Detection: the reason matches `_CART_REVIEW_RE`, the UI tree has
        ZERO `_CART_SCREEN_TOKENS` matches, AND has at least one
        `_SEARCH_SCREEN_TOKENS` match. All three conditions together avoid
        false positives on apps without explicit cart-screen text.

        Pass-through (no rejection) when the UI tree is unavailable —
        better to surface the approval than to block the flow blindly.
        """
        if action.get("action") != "need_approval":
            return None
        reason = str(action.get("reason", ""))
        if not _CART_REVIEW_RE.search(reason):
            return None
        if not elements:
            return None
        haystack = " ".join(
            (e.text + " " + e.desc).lower() for e in elements
        )
        if any(tok in haystack for tok in _CART_SCREEN_TOKENS):
            return None
        if not any(tok in haystack for tok in _SEARCH_SCREEN_TOKENS):
            # No clear search-screen indicator either — don't second-guess.
            return None
        cart_bar = next((e for e in elements if e.is_cart_bar), None)
        if cart_bar is not None:
            cart_hint = (
                f" Tap the [CART] element at ({cart_bar.cx},{cart_bar.cy}) "
                "to navigate to the cart screen first."
            )
        else:
            cart_hint = (
                " No [CART] element is visible in the UI tree on this "
                "screen — press back to return to the home screen and "
                "look for the labelled 'View cart' bar there."
            )
        return (
            "premature cart-review need_approval: the current screen has "
            "search/results markers and lacks any cart-screen markers "
            "(no 'Proceed to checkout' / 'Place order' / 'Pay' / "
            "'Select payment' / 'subtotal' / 'total' visible). The cart "
            "screen hasn't been reached yet, so 'Cart review: ...' is "
            "premature and the items in the reason are likely "
            "hallucinated from the search-results cross-sell rows."
            + cart_hint
        )

    @staticmethod
    def _type_without_focus_rejection(
        action: dict, elements: list[UiElement]
    ) -> str | None:
        """Return a rejection reason when a `type` action is emitted with no
        focused text-input element in the UI tree, else None.

        ADB's `input text` (and ADBKeyboard's broadcast variant) only land
        characters in whatever EditText is currently focused. If nothing is
        focused, the keystrokes go nowhere — but the model can't tell from
        a screenshot alone whether its previous tap landed a focus or just
        navigated. So we structurally check the dump.

        Pass-through when no tree is available (empty `elements`) — we
        already err on the side of letting actions execute when validation
        info is missing. See `has_focused_text_input`'s docstring.
        """
        if action.get("action") != "type":
            return None
        if has_focused_text_input(elements):
            return None
        return (
            "type emitted but no focused EditText / SearchView / "
            "AutoCompleteTextView is visible in the UI tree. ADB typing "
            "lands characters in the focused input; without one the "
            "keystrokes go to /dev/null. The previous tap probably "
            "navigated to a new screen (e.g., the search page) without "
            "focusing its input. Required next action: find the actual "
            "EditText/SearchView on the current screen in the UI elements "
            "list and tap its coords, then retry the type"
        )

    @staticmethod
    def _add_product_name_mismatch(
        action: dict, elements: list[UiElement]
    ) -> str | None:
        """Reject an "ADD on <product>" tap when <product> doesn't match the
        product label attached to the [ACTION] element under those coords.

        Primary signal: each [ACTION] element carries a `container_label`
        sourced from its XML ancestor's content-desc (the product card's
        full title — e.g. "Hen Fruit -10 Max Protein Speciality Eggs").
        That's the ground truth for which product the ADD button buys.

        If the tap lands on an [ACTION] whose container_label exists and
        shares NO significant keyword with the model's claimed product
        name, this is the Fire-TV-Stick-instead-of-eggs / Coolberg-Beer-
        instead-of-Hen-Fruit failure mode and we reject with the actual
        product name surfaced in the error.

        Fall back to the prior band-based "any nearby text matches" check
        when the action has no container_label (some apps don't expose
        product titles in the accessibility tree).

        Conservative on purpose:
        - skips when product name can't be parsed (no rejection)
        - skips when claimed name has no meaningful (≥3 char, non-stop)
          words (no rejection)
        - skips when UI tree is empty (no rejection)
        - matches loosely (any significant word, case-insensitive)
        """
        if action.get("action") != "tap":
            return None
        if not elements:
            return None
        note = str(action.get("note", ""))
        if not _FRESH_ADD_NOTE_RE.search(note):
            return None
        claimed = _extract_product_from_add_note(note)
        if not claimed:
            return None
        words = [
            w for w in re.split(r"[^a-z0-9]+", claimed.lower())
            if len(w) >= 3 and w not in _PRODUCT_NOISE_WORDS
        ]
        if not words:
            return None
        try:
            x = int(action.get("x"))
            y = int(action.get("y"))
        except (TypeError, ValueError):
            return None
        # First-line check: does the [ACTION] element under these coords
        # have an XML-ancestor product label that contradicts the claim?
        action_target = find_action_at(x, y, elements)
        if action_target is not None and action_target.container_label:
            label_lc = action_target.container_label.lower()
            if any(w in label_lc for w in words):
                return None
            # The label under these coords doesn't match the claim. Only
            # treat that as a real mismatch when the label is itself a
            # product name. On a multi-variant options bottom-sheet the ADD
            # button's ancestor content-desc is the option's price/offer
            # line ("quantity ₹301 rupees, offer 20% OFF"), NOT the product
            # — rejecting on it falsely blocks a legitimate variant ADD (the
            # 2026-05-27 Abhi-eggs run, rejected 4x on steps 5-8). When the
            # label is price/offer noise, trust the on-screen product title
            # instead: if the claimed product appears anywhere in the tree
            # (the sheet header), the ADD is for that product — allow it.
            if _looks_like_product_label(action_target.container_label):
                return (
                    f"note claims tap ADD on '{claimed}' at ({x},{y}), but "
                    f"the ADD button at those coords actually buys "
                    f"'{action_target.container_label}' (per the UI tree's "
                    f"product card label). Find the [ACTION] line whose `for "
                    f"\"...\"` annotation actually matches your target "
                    "product, and tap ITS coords"
                )
            tree_text = " ".join(f"{e.text} {e.desc}" for e in elements).lower()
            if any(w in tree_text for w in words):
                return None
        # Fallback: no container label available — use the legacy
        # vertically-banded "any nearby text" check so we still catch the
        # mismatch on apps where product titles aren't surfaced as
        # row-level content-desc.
        target = find_smallest_element_at(x, y, elements)
        if target is None:
            return None
        nearby = []
        for e in elements:
            if abs(e.cy - target.cy) > _PRODUCT_NEARBY_BAND_PX:
                continue
            if e.text:
                nearby.append(e.text.lower())
            if e.desc:
                nearby.append(e.desc.lower())
        if not nearby:
            return None
        haystack = " ".join(nearby)
        if any(w in haystack for w in words):
            return None
        return (
            f"note claims tap ADD on '{claimed}' at ({x},{y}), but none "
            f"of the keywords {words} appear in any text near those coords "
            f"in the UI tree. Nearest texts: {nearby[:6]}. The tap is "
            "landing on a DIFFERENT product's ADD button. Find the row "
            "whose title actually contains your target product name and "
            "use ITS [ACTION] ADD coords"
        )

    @staticmethod
    def _excess_quantity_rejection(task: Task, action: dict) -> str | None:
        """Reject an add/increment tap that would exceed the requested quantity.

        Production finding (2026-06-08 live Blinkit run): asked for "one pack
        of Amul milk", the agent tapped ADD then bumped the stepper twice,
        ending at qty 3. The prompt's "default to ONE" rule wasn't enough —
        the model fumbled the stepper. This is the structural backstop.

        Counts EXECUTED quantity-increasing taps so far (a fresh ADD reaches
        qty 1; each +/increment adds 1) and rejects the next one once the
        count has reached the requested quantity. Allows exactly `target`
        increases, so a legitimate "add 2" flow (ADD then one +) is untouched.

        Scoped to single-item tasks only (`_is_single_item_task`): on a
        multi-item task the cross-product count wouldn't equal one item's
        quantity, so the guard disarms rather than block the second item.
        Commerce-gated by the caller, so non-shopping apps never see it.
        """
        if action.get("action") != "tap":
            return None
        note = str(action.get("note", ""))
        if not _QTY_INCREASE_NOTE_RE.search(note):
            return None
        if not _is_single_item_task(task.description):
            return None
        target = _requested_quantity(task.description)
        executed_increases = 0
        for entry in task.history:
            if not isinstance(entry, dict):
                continue
            prev = entry.get("action")
            if not isinstance(prev, dict) or prev.get("action") != "tap":
                continue
            result = str(entry.get("result", ""))
            if result.startswith("REJECTED") or result.startswith("ERROR"):
                continue
            if _QTY_INCREASE_NOTE_RE.search(str(prev.get("note", ""))):
                executed_increases += 1
        if executed_increases < target:
            return None
        return (
            f"the task '{task.description}' asks for quantity {target}, and "
            f"{executed_increases} add/increment action(s) have already "
            f"executed — tapping ADD/+ again would over-add. After an item is "
            f"in the cart at the requested quantity, do NOT keep tapping the "
            f"card or the '+' stepper. Navigate to the cart (the [CART] bar or "
            f"cart icon) to review and check out."
        )

    @staticmethod
    def _repeated_add_rejection(task: Task, action: dict) -> str | None:
        """Reject ANY 'fresh ADD' tap at the same coords as a previous one.

        After an ADD button is tapped, the card transforms into a "− 1 +"
        stepper — those pixels stop being ADD. A second 'tap ADD' at the
        same coords is therefore wrong in every plausible reading:
          (a) it actually hits the new stepper '+' and silently bumps qty
              (which the user almost never asked for);
          (b) the first ADD missed, and repeating identical coords with
              identical intent won't help — diagnose differently;
          (c) it's a hallucination (model invented a "new product card"
              that doesn't exist at that pixel).

        We only count fresh-ADD intent (literal word "add" in note), so
        legitimate stepper bumps ("tap + on Maggi to set qty 2") aren't
        rejected — they don't have the word "add". Coords within 10px count
        as "same".
        """
        if action.get("action") != "tap":
            return None
        note = str(action.get("note", ""))
        if not _FRESH_ADD_NOTE_RE.search(note):
            return None
        last_tap = _last_executed_tap(task.history)
        if last_tap is None:
            return None
        prev_note = str(last_tap.get("note", ""))
        if not _FRESH_ADD_NOTE_RE.search(prev_note):
            return None
        try:
            x, y = int(action.get("x")), int(action.get("y"))
            px, py = int(last_tap.get("x")), int(last_tap.get("y"))
        except (TypeError, ValueError):
            return None
        if abs(x - px) > 10 or abs(y - py) > 10:
            return None  # different coords — no claim of "same button"

        cur_product = _extract_product_from_add_note(note)
        prev_product = _extract_product_from_add_note(prev_note)
        # Custom message for the two sub-cases. Both are rejected; we just
        # hint differently so the model knows what to do next.
        if cur_product and prev_product and cur_product != prev_product:
            return (
                f"previous tap at ({px},{py}) claimed ADD on "
                f"'{prev_product}'; this tap at ({x},{y}) claims ADD on "
                f"'{cur_product}'. Same coords cannot be the ADD button for "
                "two different products. After one ADD lands, the card "
                "becomes a '− 1 +' stepper at those coords (not a new ADD). "
                "Go to the cart icon (the previous ADD likely landed), or "
                "find a different product card's [ACTION] ADD at different "
                "coords"
            )
        return (
            f"previous tap at ({px},{py}) was already a fresh ADD; this tap "
            f"at ({x},{y}) repeats it at the same coords with another ADD "
            "note. After one ADD lands, the card's button becomes '− 1 +' "
            "— those pixels are no longer ADD. If you wanted quantity 2, "
            "emit a tap with note 'tap + on stepper' (no word 'add'). If "
            "you wanted to proceed, tap the cart icon. Don't retry the "
            "same ADD"
        )

    @staticmethod
    def _category_tap_rejection(task: Task, action: dict) -> str | None:
        """Reject category/tile/banner taps unless the task asked for browsing.

        Prompt rule 1 forbids tapping category tiles for add-to-cart flows
        because they navigate AWAY from the search results into a landing
        page. The model is told this but sometimes ignores it (this run:
        step 8 'tap maggi noodles category'). Structural enforcement:
        if the note self-identifies as a category/tile/banner tap and the
        task description doesn't ask for browsing → reject.
        """
        if action.get("action") != "tap":
            return None
        note = str(action.get("note", ""))
        if not _CATEGORY_INTENT_RE.search(note):
            return None
        if _BROWSE_TASK_RE.search(task.description):
            return None
        return (
            f"note '{note}' is a category/tile/banner tap. The task "
            f"'{task.description}' is an add-to-cart flow, not a browse "
            "flow — tapping a category leaves the product results and "
            "lands on a landing page (often gated by a 'Confirm Location' "
            "sheet). Stay on the search results: tap the [ACTION] ADD on "
            "a real product card, or swipe to scroll if none is visible"
        )

    @staticmethod
    def _intent_mismatch(action: dict, elements: list[UiElement]) -> str | None:
        """Return a rejection reason when the tap's note disagrees with the
        element actually under the coord, else None.

        Two specific traps:

        1. Search intent on a [LOCATION] element. The model's note mentions
           "search bar / box / icon" but the coord lands on the delivery-
           address header. This is the run-2 misclick — y=200 on Blinkit's
           home screen.

        2. ADD/cart intent on a non-[ACTION] element. The note claims
           "tap ADD on …" / "+ button" / "checkout" but the target isn't
           an [ACTION] element. Often the model is hallucinating: it
           describes an action it wanted to take on a screen that doesn't
           actually have the ADD button visible.

        Passes through when no UI tree is available or the action isn't a
        tap. Returns None on a clean tap so the loop continues normally.
        """
        if action.get("action") != "tap":
            return None
        if not elements:
            return None
        try:
            x = int(action.get("x"))
            y = int(action.get("y"))
        except (TypeError, ValueError):
            return None
        note = str(action.get("note", ""))
        target = find_smallest_element_at(x, y, elements)
        if target is None:
            return None  # caught by _coords_mismatch

        # Search intent: target MUST look like a search target. This is a
        # positive rule — the LOCATION-marker check is one symptom; the
        # broader truth is "if the note says search, the element must say
        # search". On Blinkit the location header is a TextView showing
        # city/address text with no 'search' token anywhere, so the
        # LOCATION-token heuristic missed it. The positive rule below
        # catches it regardless of any app-specific token vocabulary.
        if (
            _SEARCH_INTENT_RE.search(note)
            and not _ADDRESS_INTENT_RE.search(note)
        ):
            if target.is_location_header:
                return (
                    f"note '{note}' implies tapping the search bar, but the "
                    f"element at ({x},{y}) is the [LOCATION] delivery/"
                    "address header. The real search bar is a separate "
                    "element below — look for class=EditText or an id "
                    "containing 'search', usually with cy in 300–400 range"
                )
            if not _looks_like_search_target(target):
                return (
                    f"note '{note}' implies tapping a search bar/box/icon, "
                    f"but the element at ({x},{y}) is "
                    f"class='{target.class_name}' text='{target.text}' "
                    f"id='{target.resource_id}' — none of those say 'search' "
                    "and the class isn't an EditText/SearchView. The real "
                    "search target's class will contain 'EditText' / "
                    "'SearchView' / 'AutoCompleteTextView', OR its id/text/"
                    "content-desc will contain the word 'search' (e.g., "
                    "hint text 'Search for atta, butter…'). Find THAT "
                    "element in the UI elements list and use ITS coords. "
                    "Don't tap a city-name / address / banner just because "
                    "it sits near the top of the screen"
                )

        # (2) ADD/cart intent landing on a non-[ACTION] element.
        if _ADD_INTENT_RE.search(note) and not target.is_action:
            target_kind = (
                "[CATEGORY?] tile"
                if (target.clickable and target.is_category_like)
                else "non-action element"
            )
            return (
                f"note '{note}' implies tapping an ADD/+/checkout button, "
                f"but the element at ({x},{y}) is a {target_kind} (not "
                "marked [ACTION]). Pick the exact coords of an [ACTION]-"
                "prefixed element from the UI list. If no [ACTION] ADD "
                "button is visible for the target product, swipe to scroll "
                "and re-evaluate — don't tap on a category, image, or "
                "label hoping it acts as ADD"
            )

        return None

    @staticmethod
    def _coords_mismatch(action: dict, elements: list[UiElement]) -> str | None:
        """Return a human-readable reason if action's coords don't map to any
        element in the UI tree, or None when the action's coords are fine.

        Non-coord actions (wait / done / need_approval / type) pass through.
        Empty element list also passes through — without a tree to compare
        against we can't reject anything.
        """
        if not elements:
            return None
        action_type = action.get("action")
        if action_type == "tap":
            try:
                x = int(action.get("x"))
                y = int(action.get("y"))
            except (TypeError, ValueError):
                return None  # validator already caught malformed; don't double-fail
            if not is_coord_in_elements(x, y, elements):
                return f"tap at ({x}, {y}) doesn't fall on any UI element"
        elif action_type == "swipe":
            try:
                x1 = int(action.get("x1"))
                y1 = int(action.get("y1"))
            except (TypeError, ValueError):
                return None
            if not is_coord_in_elements(x1, y1, elements):
                return (
                    f"swipe start ({x1}, {y1}) doesn't fall on any UI element"
                )
        return None

    def _loop_hint(self, task: Task) -> str:
        """Detect repeated-action and cycle patterns; return a hint or ''.

        Three independent detectors:
          1. A-A           : last two state-changing actions identical
          2. A-B-A-B       : last four form a two-step alternation
          3. Same-action seen 3+ times anywhere in history (catches 3-step
             cycles like A-B-C-A-B-C and any other repetition shape)
        Any detector firing increments self._loops_detected; the caller
        hard-aborts when that crosses MAX_LOOPS_BEFORE_ABORT.
        """
        # Only actions that actually EXECUTED count toward loop detection.
        # Rejected actions (stale-tree / coord / intent rejections) get
        # re-emitted by the model precisely because WE refused to run them —
        # counting those as "loops" makes the loop detector fight the
        # stale-tree escape hatch: on an un-dumpable screen the model is
        # forced to repeat the same (correct) tap, and the 4-loop abort
        # trips at almost the exact step the escape hatch would finally let
        # the tap through (the 2026-05-27 "detected 4 action loops" Blinkit
        # aborts). A genuine loop is repeated *executed* actions that don't
        # advance the screen, so skip entries whose result was a rejection.
        recent_state_changing = [
            h.get("action", {})
            for h in task.history
            if isinstance(h.get("action"), dict)
            and h["action"].get("action") in _STATE_CHANGING
            and not str(h.get("result", "")).strip().upper().startswith("REJECTED")
        ]
        if len(recent_state_changing) < 2:
            return ""

        last_two = recent_state_changing[-2:]
        last_four = recent_state_changing[-4:]

        is_aa = _actions_equivalent(last_two[0], last_two[1])
        is_abab = (
            len(last_four) == 4
            and _actions_equivalent(last_four[0], last_four[2])
            and _actions_equivalent(last_four[1], last_four[3])
            and not _actions_equivalent(last_four[0], last_four[1])
        )
        # The most-recent action has been emitted at least 3 times somewhere
        # in this task's history — catches longer cycles + general repetition.
        latest = recent_state_changing[-1]
        repeat_count = sum(
            1 for a in recent_state_changing if _actions_equivalent(a, latest)
        )
        is_triplet = repeat_count >= 3

        if not (is_aa or is_abab or is_triplet):
            return ""

        self._loops_detected += 1
        self._last_loop_step = task.step_count
        if is_aa:
            shape = "identical"
        elif is_abab:
            shape = "alternating A↔B cycle"
        else:
            shape = f"repeated {repeat_count} times"
        remaining = max(0, MAX_LOOPS_BEFORE_ABORT - self._loops_detected)
        return (
            f"NOTE: Your last action has the pattern '{shape}' and isn't "
            "advancing the screen. STOP this loop. Required next step: pick a "
            "DIFFERENT element from the UI list (prefer one marked [ACTION] "
            "over a card body), or swipe to scroll, or press back to dismiss "
            "an overlay. DO NOT emit need_approval — that's reserved for "
            "payment/OTP/delete/permission and the cart-review handoff only. "
            f"If you loop {remaining} more time(s) the task will be hard-"
            "terminated."
        )

    # ------------------------------------------------------------------
    # HITL gate (keyword + vision)

    async def _gate_with_hitl(
        self,
        task: Task,
        action: dict,
        screenshot: bytes,
        screenshot_b64: str,
        screenshot_phash: str | None,
    ) -> None:
        policy = (
            self._users.policy_for(task.user_id).value
            if self._users is not None
            else "confirm_sensitive"
        )
        try:
            needs_approval = await self._hitl.requires_approval(
                action, screenshot_b64, policy
            )
        except ReadOnlyViolation as e:
            self._audit.log_action(
                task.user_id, task.description, action, f"BLOCKED: {e}"
            )
            raise OrchestratorError(f"read-only policy: {e}")

        # Vision-augmented check OR's into the keyword check. We only run it
        # for state-changing actions on the standard policy — terminal actions
        # and read_only / always_approve users are already handled above.
        if (
            self._enable_vision_hitl
            and not needs_approval
            and policy == "confirm_sensitive"
            and action.get("action") in _STATE_CHANGING
            and screenshot_phash is not None
            and hasattr(self._vision, "classify_yes_no")
        ):
            vision_says = await self._hitl.classify_sensitivity(
                screenshot, screenshot_phash, self._vision  # type: ignore[arg-type]
            )
            if vision_says:
                needs_approval = True
                self._audit.log_action(
                    task.user_id, task.description, action, "VISION_HITL_TRIGGERED"
                )

        if not needs_approval:
            return

        # Combined cart+payment approval: if the user already approved the
        # cart review (which the prompt explicitly tells the model to phrase
        # as covering payment too), payment-flow gates further down the
        # checkout (Pay Now / Place Order / proceed to payment) auto-grant.
        # This keeps the user to one approval per order while preserving the
        # audit trail. OTP, "no payment method", and other separate sensitive
        # categories don't match _PAYMENT_FLOW_RE so still prompt.
        reason_text = str(action.get("reason", ""))
        action_note = str(action.get("note", ""))
        payment_haystack = f"{reason_text} {action_note}"
        if (
            task.payment_pre_approved
            and _PAYMENT_FLOW_RE.search(payment_haystack)
            and not _CART_REVIEW_RE.search(reason_text)
        ):
            self._audit.log_action(
                task.user_id, task.description, action,
                "AUTO_APPROVED: payment pre-approved at cart review",
            )
            return

        # Unattended scheduled auto-pay (#5): the schedule opted into paying
        # automatically, so auto-grant the cart-review AND payment gates to let
        # the order complete with nobody watching Telegram. NEVER auto-grants
        # OTP / permission / delete / "no payment method" — those still require
        # a human and will hit the bounded wait below and time out safely.
        if (
            task.auto_approve_payment
            and action.get("action") == "need_approval"
            and (
                _CART_REVIEW_RE.search(reason_text)
                or _PAYMENT_FLOW_RE.search(payment_haystack)
            )
            and not _NON_AUTOPAY_RE.search(payment_haystack)
        ):
            if _CART_REVIEW_RE.search(reason_text):
                task.payment_pre_approved = True
            self._audit.log_action(
                task.user_id, task.description, action,
                "AUTO_APPROVED: scheduled auto-pay (cart/payment)",
            )
            return

        # If the model emits need_approval shortly after a loop hint was
        # injected, it's *usually* using approval as a give-up escape hatch
        # rather than a genuine sensitive-action gate. EXCEPTION: a
        # legitimate-sensitive reason (cart review, payment, OTP, no
        # payment method, address confirmation, delete, permission) is a
        # real handoff even if a wobble happened a couple of steps before.
        # We previously killed all post-loop need_approval indiscriminately,
        # which dropped real cart-review handoffs on the floor.
        if (
            self._last_loop_step >= 0
            and (task.step_count - self._last_loop_step) <= LOOP_TO_GIVEUP_WINDOW
            and action.get("action") == "need_approval"
        ):
            reason = str(action.get("reason", ""))
            if not _LEGITIMATE_SENSITIVE_RE.search(reason):
                self._audit.log_action(
                    task.user_id, task.description, action,
                    "BLOCKED: need_approval used as loop escape",
                )
                raise OrchestratorError(
                    "model emitted need_approval to escape a detected loop; "
                    "aborting instead of pausing for approval"
                )
            # Legitimate handoff inside the loop window — log and fall
            # through to normal HITL flow.
            self._audit.log_action(
                task.user_id, task.description, action,
                "LOOP_WINDOW: legitimate sensitive reason — passing through",
            )

        task.state = TaskState.AWAITING_APPROVAL
        task.pending_action = action
        self._audit.log_action(
            task.user_id, task.description, action, "AWAITING_APPROVAL"
        )
        await self._request_approval(task, action)
        approved = await self._hitl.wait_for_approval(
            task.user_id, timeout=self._approval_timeout
        )
        if not approved:
            self._audit.log_action(
                task.user_id, task.description, action, "DENIED"
            )
            raise OrchestratorError(
                "approval not granted (denied or timed out); nothing was "
                "paid — cart left for review"
                if self._approval_timeout is not None
                else "user denied approval"
            )
        # If the user just approved a Cart-review need_approval, latch the
        # combined-approval flag so the downstream Pay Now / Place Order
        # gate auto-grants. The prompt instructs the model to phrase the
        # cart-review reason as covering payment, so a single user tap on
        # Approve covers the whole checkout. (We still HITL OTPs and other
        # unrelated sensitive categories separately.)
        if action.get("action") == "need_approval" and _CART_REVIEW_RE.search(
            str(action.get("reason", ""))
        ):
            task.payment_pre_approved = True
            self._audit.log_action(
                task.user_id, task.description, action,
                "CART_APPROVED: payment_pre_approved=True",
            )
        task.state = TaskState.RUNNING
        task.pending_action = None

    # ------------------------------------------------------------------
    # Execution: retry + outcome verification

    async def _execute_with_retry(self, task: Task, action: dict) -> str:
        last_err: Exception | None = None
        for attempt, backoff in enumerate((0.0,) + RETRY_BACKOFF_SECONDS):
            if backoff:
                await asyncio.sleep(backoff)
            try:
                return await execute_action(action, self._adb)
            except AdbError as e:
                last_err = e
                _log.warning(
                    "adb action %s failed (attempt %d): %s",
                    action.get("action"), attempt + 1, e,
                )
                # Append a failure marker so the next provider call sees that
                # the previous attempt didn't land.
                task.history.append(
                    {"action": action, "result": f"ERROR: {e}", "previous_attempt_failed": True}
                )
                self._audit.log_action(
                    task.user_id, task.description, action, f"RETRY[{attempt + 1}]: {e}"
                )
        raise OrchestratorError(f"adb action failed after retries: {last_err}")

    async def _verify_outcome(
        self, task: Task, action: dict, pre_phash: str | None
    ) -> None:
        try:
            post = await self._adb.screencap()
        except AdbError:
            # Flaky adb shouldn't kill the task — let the next iter retake.
            return
        post_phash = await _safe_phash(post)
        if pre_phash is None or post_phash is None or post_phash != pre_phash:
            self._unchanged_streak = 0
            return

        self._unchanged_streak += 1
        if self._unchanged_streak >= UNCHANGED_STREAK_FAIL:
            raise OrchestratorError(
                f"screen unchanged after {self._unchanged_streak} actions; aborting"
            )
        if self._unchanged_streak >= UNCHANGED_STREAK_RECOVERY:
            try:
                await self._adb.key_event(KEYCODE_BACK)
            except AdbError as e:
                self._audit.log_action(
                    task.user_id, task.description, action,
                    f"RECOVERY_FAILED: back-button: {e}",
                )
                return
            self._audit.log_action(
                task.user_id, task.description, action, "RECOVERY: back-button"
            )

    # ------------------------------------------------------------------
    # Utilities

    async def _safe_screen_size(self) -> tuple[int, int] | None:
        try:
            return await self._adb.get_screen_size()
        except Exception:
            return None

    @staticmethod
    def _record_usage(task: Task, usage) -> None:
        task.total_input_tokens += int(getattr(usage, "input_tokens", 0) or 0)
        task.total_output_tokens += int(getattr(usage, "output_tokens", 0) or 0)
        rpd = getattr(usage, "rpd_remaining", None)
        if rpd is not None:
            task.latest_rpd_remaining = int(rpd)

    async def _maybe_warn_low_rpd(self, task: Task) -> None:
        if self._low_rpd_warned:
            return
        rpd = task.latest_rpd_remaining
        if rpd is None or rpd > LOW_RPD_WARNING_THRESHOLD:
            return
        self._low_rpd_warned = True
        await self._status(task, f"⚠️ low daily quota: {rpd} requests remaining")

    async def _request_approval(self, task: Task, action: dict) -> None:
        # If the approval message can't be delivered (Telegram outage), the
        # subsequent wait_for_approval will time out cleanly — no need to
        # crash the task with the network error.
        if self.on_approval_request is None:
            return
        try:
            await self.on_approval_request(task, action)
        except Exception:
            pass

    async def _status(self, task: Task, message: str) -> None:
        """Lifecycle / error messages — best-effort, never fatal.

        A Telegram outage or transient timeout must not abort an in-flight
        task. We swallow status-callback exceptions; the agent loop keeps
        running and the user can /status the task later.
        """
        if self.on_status_update is None:
            return
        try:
            await self.on_status_update(task, message)
        except Exception:
            pass

    async def _step_status(self, task: Task, message: str) -> None:
        """Per-step progress — throttled + best-effort (same rationale)."""
        if self.on_status_update is None:
            return
        now = time.monotonic()
        if now - self._last_step_status_at < STEP_STATUS_MIN_INTERVAL_SECONDS:
            return
        self._last_step_status_at = now
        try:
            await self.on_status_update(task, message)
        except Exception:
            pass


    def _make_task_artifact_dir(self, task: Task) -> Path | None:
        """Create a per-task directory under self._artifact_dir and return it.

        Returns None when persistence is disabled (`artifact_dir` None at
        construction) or when the directory can't be created — in either
        case the loop will silently skip per-step saves. Never raises.
        """
        if self._artifact_dir is None:
            return None
        try:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%SZ")
            slug = _safe_slug(task.description)
            sub = self._artifact_dir / f"{stamp}_user{task.user_id}_{slug}"
            sub.mkdir(parents=True, exist_ok=True)
            # Drop a task.json so reviewers know what the user asked for.
            try:
                (sub / "task.json").write_text(
                    json.dumps(
                        {
                            "user_id": task.user_id,
                            "description": task.description,
                            "started_at": stamp,
                        },
                        indent=2,
                    )
                )
            except OSError:
                pass
            return sub
        except OSError:
            return None

    def _emit_step(self, task: Task, action: dict, result: str, *, phase: str) -> None:
        """Publish a per-step event to the dashboard event bus (best-effort).

        Sync + non-blocking (the bus's publish never awaits/raises), so it can
        never stall the agent loop. No-op when no bus is wired.
        """
        if self._event_bus is None:
            return
        a = action or {}
        atype = a.get("action")
        coords: dict = {}
        if atype == "tap":
            coords["tap"] = {"x": a.get("x"), "y": a.get("y")}
        elif atype == "swipe":
            coords["swipe"] = {k: a.get(k) for k in ("x1", "y1", "x2", "y2")}
        elif atype == "type":
            coords["text"] = a.get("text")
        db_id = self._current_task_db_id
        screenshot_url = (
            f"/api/tasks/{db_id}/steps/{task.step_count}/screenshot"
            if db_id is not None
            else None
        )
        try:
            self._event_bus.publish({
                "type": "step",
                "task_id": db_id,
                "user_id": task.user_id,
                "step": task.step_count,
                "phase": phase,
                "action_type": atype,
                "note": str(a.get("note", "")),
                "coords": coords,
                "result": result,
                "rejected": isinstance(result, str) and result.startswith("REJECTED"),
                "state": task.state.value,
                "screenshot_url": screenshot_url,
                "tokens": {
                    "in": task.total_input_tokens,
                    "out": task.total_output_tokens,
                },
                "ts": datetime.now(timezone.utc).isoformat(),
            })
        except Exception:
            pass

    def _emit_state(self, task: Task) -> None:
        """Publish a task lifecycle (state) event to the dashboard bus."""
        if self._event_bus is None:
            return
        try:
            self._event_bus.publish({
                "type": "state",
                "task_id": self._current_task_db_id,
                "user_id": task.user_id,
                "state": task.state.value,
                "step": task.step_count,
                "description": task.description,
                "final_summary": task.final_summary,
                "failure_reason": task.failure_reason,
                "ts": datetime.now(timezone.utc).isoformat(),
            })
        except Exception:
            pass

    def _finalize_step_result(
        self, step: int, action: dict | None, result: str
    ) -> None:
        """Overwrite the step's .json with its FINAL action+result.

        Called at every exit of an iteration (rejection or execution). The
        initial save happened right after the vision call with
        result='(pending)'; this updates it once we know what actually
        happened. Screenshot and XML are not re-saved — they're already on
        disk from the pending write.
        """
        self._save_step_artifacts(step, None, None, action, result)

    def _save_step_artifacts(
        self,
        step: int,
        screenshot: bytes | None,
        ui_xml: str | None,
        action: dict | None,
        result: str | None,
        ui_prompt: str | None = None,
    ) -> None:
        """Write screenshot/XML/action JSON for `step`. Best-effort.

        Files: step_NN.png, step_NN.xml, step_NN.json, step_NN.ui_prompt.txt
        Anything that fails to write is silently skipped — debugging
        artifacts must never abort a live task.
        """
        sub = self._current_task_artifact_dir
        if sub is None:
            return
        prefix = f"step_{step:02d}"
        if screenshot:
            try:
                (sub / f"{prefix}.png").write_bytes(screenshot)
            except OSError:
                pass
        if ui_xml:
            try:
                (sub / f"{prefix}.xml").write_text(ui_xml)
            except OSError:
                pass
        if ui_prompt:
            try:
                (sub / f"{prefix}.ui_prompt.txt").write_text(ui_prompt)
            except OSError:
                pass
        if action is not None or result is not None:
            try:
                (sub / f"{prefix}.json").write_text(
                    json.dumps(
                        {"action": action, "result": result},
                        indent=2,
                        default=str,
                    )
                )
            except OSError:
                pass

    async def _persist_insert(self, task: Task) -> None:
        if self._repo is None:
            return
        try:
            self._current_task_db_id = await self._repo.insert_task(task)
        except Exception:
            # Persistence is best-effort — never let a DB hiccup kill a task.
            self._current_task_db_id = None

    async def _persist_update(self, task: Task) -> None:
        if self._repo is None or self._current_task_db_id is None:
            return
        try:
            await self._repo.update_state(self._current_task_db_id, task)
        except Exception:
            pass

    async def _persist_step(self, task: Task, action: dict, result: str) -> None:
        if self._repo is None or self._current_task_db_id is None:
            return
        try:
            await self._repo.append_step(
                self._current_task_db_id, task.step_count, action, result
            )
        except Exception:
            pass


def _summarize_report(report: dict) -> str:
    """One-line human summary of a probe `report` payload.

    Used as the task's final_summary when the model didn't supply its own
    `summary` string on a `report` action. Tolerant of missing keys — a probe
    that found nothing still produces a readable line.
    """
    if not report:
        return "no result"
    name = str(report.get("item_name") or "").strip()
    if report.get("available") is False:
        return f"{name or 'item'}: unavailable"
    bits: list[str] = []
    price = report.get("price")
    if price is not None:
        currency = str(report.get("currency") or "INR").strip()
        symbol = "₹" if currency.upper() == "INR" else f"{currency} "
        bits.append(f"{symbol}{price}")
    eta = str(report.get("eta") or "").strip()
    if eta:
        bits.append(eta)
    if name:
        bits.append(name)
    return " · ".join(bits) if bits else "no result"


def _repeated_rejection_count(history: list[dict]) -> int:
    """Count trailing entries that are the SAME rejected action.

    A rejected step has a result starting "REJECTED" and was never executed, so
    loop detection (which only sees executed actions) misses it. This lets the
    orchestrator notice a model stuck re-proposing one guard-rejected action and
    bail before wasting the whole budget. Counts back from the most recent entry
    until a non-rejected step or a different action breaks the streak.
    """
    streak = 0
    anchor: dict | None = None
    for h in reversed(history):
        if not str(h.get("result", "")).startswith("REJECTED"):
            break
        action = h.get("action", {}) or {}
        if anchor is None:
            anchor = action
            streak = 1
        elif _actions_equivalent(action, anchor):
            streak += 1
        else:
            break
    return streak


def _taps_since_last_type(history: list[dict]) -> int:
    """Executed taps after the most recent executed `type`.

    The read-only probe guard uses this to decide whether the model actually
    opened a result (tapped something after searching) before claiming no
    price. Rejected/errored steps don't count as executed.
    """
    def _executed(entry: dict) -> bool:
        res = str(entry.get("result", ""))
        return not (res.startswith("REJECTED") or res.startswith("ERROR"))

    last_type = -1
    for i, h in enumerate(history):
        action = h.get("action", {}) or {}
        if action.get("action") == "type" and _executed(h):
            last_type = i
    if last_type < 0:
        return 0
    return sum(
        1
        for h in history[last_type + 1:]
        if (h.get("action", {}) or {}).get("action") == "tap" and _executed(h)
    )


def _actions_equivalent(a: dict, b: dict) -> bool:
    """True if two actions are 'the same effective gesture'.

    For tap/swipe we compare coords (with a tiny tolerance — pixel-perfect
    repeats are rare so any few-pixel jitter still counts as a loop). For
    type we compare the text. Anything else compares by action type only.
    """
    if a.get("action") != b.get("action"):
        return False
    t = a.get("action")
    if t == "tap":
        return abs(int(a.get("x", 0)) - int(b.get("x", 0))) <= 5 \
            and abs(int(a.get("y", 0)) - int(b.get("y", 0))) <= 5
    if t == "type":
        return str(a.get("text", "")) == str(b.get("text", ""))
    if t == "swipe":
        return all(
            abs(int(a.get(k, 0)) - int(b.get(k, 0))) <= 5
            for k in ("x1", "y1", "x2", "y2")
        )
    return True


def _history_has_swipe(history: list[dict]) -> bool:
    """True if the task has executed at least one swipe action.

    Used by the giveup-rejection check to decide whether the model has
    earned the right to emit 'no products found'. A rejected-coord or
    rejected-giveup entry isn't a swipe; we look only at the action's
    `action` field.
    """
    for entry in history:
        action = entry.get("action") if isinstance(entry, dict) else None
        if isinstance(action, dict) and action.get("action") == "swipe":
            # Skip swipes that never actually executed (e.g. rejected by
            # coord grounding). A rejected entry has a result starting
            # with "REJECTED".
            result = str(entry.get("result", ""))
            if not result.startswith("REJECTED"):
                return True
    return False


def _is_executed_tap(entry: object) -> bool:
    """True if `entry` is a history record of a tap that actually ran."""
    if not isinstance(entry, dict):
        return False
    action = entry.get("action")
    if not isinstance(action, dict) or action.get("action") != "tap":
        return False
    result = str(entry.get("result", ""))
    return not (result.startswith("REJECTED") or result.startswith("ERROR"))


def _history_has_executed_add(history: list[dict]) -> bool:
    """True if an add/increment tap has actually executed in this task.

    Signals an item is in the cart, so the post-ADD cart-reveal hint can fire.
    Uses the add/increment note vocabulary (`_ADD_TAP_NOTE_RE`); rejected and
    errored taps don't count.
    """
    for entry in history:
        if _is_executed_tap(entry) and _ADD_TAP_NOTE_RE.search(
            str(entry["action"].get("note", ""))
        ):
            return True
    return False


def _swipe_after_last_add(history: list[dict]) -> bool:
    """True if an executed swipe came AFTER the last executed add/increment tap.

    Lets the cart-reveal hint escalate: swipe-up first, and only switch to
    "press back to home" once a scroll has already been tried since the ADD.
    """
    last_add_idx = -1
    for i, entry in enumerate(history):
        if _is_executed_tap(entry) and _ADD_TAP_NOTE_RE.search(
            str(entry["action"].get("note", ""))
        ):
            last_add_idx = i
    if last_add_idx < 0:
        return False
    for entry in history[last_add_idx + 1:]:
        action = entry.get("action") if isinstance(entry, dict) else None
        if isinstance(action, dict) and action.get("action") == "swipe":
            if not str(entry.get("result", "")).startswith("REJECTED"):
                return True
    return False


# Class-name substrings that mark an element as the real search target.
# Used by `_looks_like_search_target` to validate that a "search bar"-noted
# tap actually lands on something that can accept a search query.
_SEARCH_CLASS_TOKENS = ("edittext", "searchview", "autocompletetextview")


def _looks_like_search_target(e: UiElement) -> bool:
    """True if `e` is plausibly THE search input on screen.

    Positive heuristic — must satisfy at least one of:
      - class contains EditText / SearchView / AutoCompleteTextView
      - resource-id contains 'search' (e.g., search_box, search_button)
      - visible text contains 'search' (typically hint text like
        'Search for atta, butter…' — present on home-screen launchers
        that aren't real inputs but DO navigate to a search screen)
      - content-desc contains 'search' (icon-only search buttons)

    Anything that satisfies NONE of the above is not a search target — it
    might be a location header, a banner, a category tile, the user's
    profile chip, etc.
    """
    cls = e.class_name.lower()
    if any(tok in cls for tok in _SEARCH_CLASS_TOKENS):
        return True
    if "search" in e.resource_id.lower():
        return True
    if "search" in e.text.lower():
        return True
    if "search" in e.desc.lower():
        return True
    return False


def _requested_quantity(description: str) -> int:
    """Parse the quantity the task explicitly asks for; default 1.

    Recognises "2 packs" / "qty 3" / "add 2" / "two packets" / "3 of". Ignores
    sizes ("500ml", "70g") because those numbers aren't followed by a count
    unit. Clamped to [1, _MAX_REQUESTED_QTY] so a stray number can't set an
    absurd target. Default 1 encodes the prompt's "if unspecified, add ONE".
    """
    d = description.lower()
    best = 0
    for rx in (_QTY_DIGIT_UNIT_RE, _QTY_IMPERATIVE_RE, _QTY_OF_RE, _QTY_KEYWORD_RE):
        m = rx.search(d)
        if m:
            try:
                best = max(best, int(m.group(1)))
            except ValueError:
                pass
    mw = _QTY_WORD_UNIT_RE.search(d)
    if mw:
        best = max(best, _QTY_WORDS.get(mw.group(1), 1))
    if best <= 0:
        return 1
    return min(best, _MAX_REQUESTED_QTY)


def _is_single_item_task(description: str) -> bool:
    """True when the task names a single item (no conjunction/list separator).

    The excess-quantity guard counts add/increment actions across the whole
    task; that equals one product's quantity only for a single-item task, so
    the guard disarms on multi-item tasks ("add milk AND bread") to avoid
    blocking the second item's ADD.

    A trailing checkout-action clause is stripped first, so "add milk and
    proceed to checkout" is still single-item (the "and" joins an action, not
    a second product) and the guard stays armed — the fix for the live run
    where milk over-added under exactly that phrasing.
    """
    core = _TRAILING_ACTION_CLAUSE_RE.sub("", description)
    return _MULTI_ITEM_RE.search(core) is None


def _looks_like_product_label(label: str) -> bool:
    """True if `label` reads like a product name, not price/offer/qty text.

    A real product label has at least one alphabetic word (≥3 chars) that
    isn't a pricing/measurement/offer keyword:
      "Coolberg Cranberry Non-Alcoholic Beer" → True
      "Abhi Vitamin D3 White Protein Rich Eggs Box" → True
      "quantity  ₹301 rupees , offer 20% OFF" → False  (variant-sheet noise)

    Used by `_add_product_name_mismatch` to avoid rejecting a legitimate ADD
    on a multi-variant options sheet, where the ADD button's ancestor
    content-desc is the option's price/offer line rather than the product.
    """
    for w in re.split(r"[^a-z]+", label.lower()):
        if (
            len(w) >= 3
            and w not in _PRICE_OFFER_NOISE_WORDS
            and w not in _PRODUCT_NOISE_WORDS
        ):
            return True
    return False


def _extract_product_from_add_note(note: str) -> str | None:
    """Pull the product-name fragment out of a 'tap ADD on …' note.

    Returns a normalized lowercase string (whitespace-collapsed, trailing
    'card'/'button' trimmed) or None if the note doesn't match the shape.
    Used by `_repeated_add_rejection` to tell whether two consecutive ADD
    taps name the SAME product (legitimate retry — let loop detector
    handle) or DIFFERENT products (hallucination — reject immediately).
    """
    m = _ADD_NOTE_PRODUCT_RE.search(note)
    if not m:
        return None
    name = m.group(1).strip().strip(",.:;\"'")
    name = re.sub(r"\s+", " ", name).lower()
    # Trim common trailing fluff so "Maggi Noodles card" == "Maggi Noodles".
    for tail in (" card", " button", " row", " item"):
        if name.endswith(tail):
            name = name[: -len(tail)].rstrip()
    return name or None


def _last_executed_tap(history: list[dict]) -> dict | None:
    """Return the most recent successfully-executed tap action, or None.

    Used by the giveup-rejection's contradiction check. We skip entries
    whose result starts with "REJECTED" or "ERROR:" — those represent
    actions that didn't actually run on the device.
    """
    for entry in reversed(history):
        if not isinstance(entry, dict):
            continue
        action = entry.get("action")
        if not isinstance(action, dict) or action.get("action") != "tap":
            continue
        result = str(entry.get("result", ""))
        if result.startswith("REJECTED") or result.startswith("ERROR:"):
            continue
        return action
    return None


def _safe_slug(s: str, max_len: int = 32) -> str:
    """Filesystem-safe slug for use in directory names.

    Keeps letters / digits / dashes; collapses other runs to a single '-'.
    Truncates at `max_len` so deeply-nested directories stay reasonable.
    Falls back to 'task' for entirely-non-alphanumeric inputs.
    """
    cleaned = re.sub(r"[^A-Za-z0-9-]+", "-", s).strip("-").lower()
    if not cleaned:
        return "task"
    return cleaned[:max_len].rstrip("-") or "task"


async def _safe_phash(image_bytes: bytes) -> str | None:
    """Compute the screenshot phash off the event loop.

    phash decodes the full PNG and runs a DCT — tens to >100ms of pure CPU on
    a multi-megapixel screenshot, twice per step (dedup + outcome verify).
    Running it inline blocks the single asyncio loop that also drives Telegram
    polling, the trigger webhook, and any concurrent user's task, so offload it
    to a worker thread. The hash value is identical to the inline computation.
    """
    try:
        return await asyncio.to_thread(compute_phash, image_bytes)
    except ValueError:
        return None
