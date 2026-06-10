"""Telegram callbacks: menu-driven app/task flow + pairing + admin + approval."""
from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, ContextTypes

from agent.comparison import ComparisonEngine, ComparisonResult, ProbeTarget
from agent.orchestrator import Orchestrator
from agent.persistence import (
    SavedTaskRepository,
    SavedTaskRow,
    ScheduleRepository,
    ScheduleRow,
    TaskRepository,
)
from agent.state_machine import Task, TaskState
from bot.apps import (
    APPS,
    App,
    TaskTemplate,
    get_app,
    get_task,
    render_prompt,
)
from bot.intent import (
    CompareIntent,
    IntentClassifier,
    RunSavedIntent,
    SaveIntent,
    ScheduleIntent,
    SingleIntent,
)
from bot.pairing import PairCodeIssuer
from bot.router import Route, Router
from bot.scheduler import build_schedule_spec, describe_schedule
from bot.session import Session, SessionState, SessionStore
from bot.users import UserPolicy, UserRecord, UserStore
from security.hitl_gate import HitlGate


# Inline-keyboard layout knobs.
_APP_BUTTONS_PER_ROW = 3
_TASK_BUTTONS_PER_ROW = 2

# An external trigger (Android voice/widget) proposes a task and waits for a
# spoken yes/no. The proposal expires after this long so a stale "yes" can't
# fire something the user said minutes ago.
_PENDING_TTL_SECONDS = 90

# A finished comparison keeps its "order from which app?" buttons live this
# long. Longer than _PENDING_TTL_SECONDS because the user may take a while to
# read a ranked table and decide before tapping an app.
_COMPARISON_TTL_SECONDS = 600

# Task ids that represent the orderable/primary action for an app, in priority
# order. Used to pick which task to run when the user selects an app from a
# comparison (groceries/food -> order, shopping -> search, mobility -> book).
_ORDER_TASK_PRIORITY = ("order", "book", "search", "play", "nav", "recharge")

# How long an unattended scheduled run waits at a cart/payment HITL gate before
# giving up (stop-at-cart default: nothing is paid if no one approves in time).
_SCHEDULED_APPROVAL_TIMEOUT_SECONDS = 600.0

# Wake-word prefixes the trigger client (AutoVoice "Atlas") prepends to every
# spoken task. We strip them here so the router prompt sees just the command
# (e.g. "order milk on blinkit" instead of "atlas order milk on blinkit").
# Longer variants must come first — for matching they don't overlap, but the
# order keeps the obvious "longest first" reading. STT delivers lowercase
# without punctuation; case-insensitive matching is in the helper.
_WAKE_PREFIXES: tuple[str, ...] = (
    "okay atlas",
    "ok atlas",
    "hey atlas",
    "atlas",
)


def _strip_wake_word(text: str) -> str:
    """Strip a leading wake word (atlas / hey atlas / ok atlas / okay atlas).

    Case-insensitive; requires a word boundary so we don't eat 'atlas' inside
    'atlassian'. Drops any trailing punctuation/whitespace between the wake
    word and the command. Returns the input unchanged when no wake word is
    detected — so phone-side filter changes don't require a code change.
    """
    stripped = text.lstrip()
    if not stripped:
        return text
    lower = stripped.lower()
    for prefix in _WAKE_PREFIXES:
        if not lower.startswith(prefix):
            continue
        after = stripped[len(prefix):]
        # Word-boundary: end-of-string or a non-alphanumeric next char.
        # Otherwise "atlas" would be stripped from "atlassian".
        if after and after[0].isalnum():
            continue
        return after.lstrip(" ,.\t")
    return text


@dataclass
class _PendingIntent:
    """A parsed-but-not-yet-launched task awaiting voice confirmation.

    `compare` is set when the pending item is a cross-app comparison rather than
    a single task — handle_external_confirm launches the comparison flow instead
    of a direct run_task.
    """

    description: str
    launch_package: str | None
    created_at: float
    compare: CompareIntent | None = None


@dataclass
class _PendingComparison:
    """A finished comparison whose 'order from which app?' buttons are live."""

    result: ComparisonResult
    comparison_id: int | None
    created_at: float


@dataclass
class _LastRun:
    """The most recent launch for a user — what "save this" / the post-run
    save-offer captures. Structured fields enable template-aware replay;
    `description` is the always-present fallback.
    """

    description: str
    launch_package: str | None = None
    app_id: str | None = None
    task_id: str | None = None
    param: str | None = None


class Handlers:
    """Bundles all Telegram callbacks. One instance per running bot."""

    def __init__(
        self,
        application: Application,
        orchestrator: Orchestrator,
        hitl: HitlGate,
        users: UserStore,
        pairing: PairCodeIssuer,
        admin_id: int | None,
        repo: TaskRepository | None = None,
        router: Router | None = None,
        classifier: IntentClassifier | None = None,
        comparison: ComparisonEngine | None = None,
        saved: SavedTaskRepository | None = None,
        schedules: ScheduleRepository | None = None,
        timezone_name: str = "Asia/Kolkata",
        event_bus=None,
    ) -> None:
        self._app = application
        self._orch = orchestrator
        self._hitl = hitl
        self._users = users
        self._pairing = pairing
        self._admin_id = admin_id
        self._repo = repo
        self._router = router
        # Intent classifier (wraps the router; adds comparison detection). When
        # present it supersedes the bare router on the message + voice paths.
        self._classifier = classifier
        self._comparison = comparison
        # Saved quick tasks (#4). None disables save/run-saved.
        self._saved = saved
        # Recurring/scheduled tasks (#5). None disables scheduling.
        self._schedules = schedules
        self._tz = timezone_name
        # Dashboard event bus (#6 viz). None when the dashboard is off.
        self._event_bus = event_bus
        self._sessions = SessionStore()
        self._running: dict[int, asyncio.Task] = {}
        # External-trigger proposals awaiting a spoken yes/no, keyed by user.
        self._pending: dict[int, _PendingIntent] = {}
        # Finished comparisons whose order buttons are still live, keyed by user.
        self._pending_comparison: dict[int, _PendingComparison] = {}
        # The user's most recent launch, for "save this as a quick task".
        self._last_run: dict[int, _LastRun] = {}

    # ------------------------------------------------------------------
    # Auth helpers

    def _is_paired(self, user_id: int | None) -> bool:
        return user_id is not None and self._users.is_allowed(user_id)

    def _is_admin(self, user_id: int | None) -> bool:
        return (
            self._admin_id is not None
            and user_id is not None
            and user_id == self._admin_id
        )

    def _has_running_task(self, user_id: int) -> bool:
        existing = self._running.get(user_id)
        return existing is not None and not existing.done()

    # ------------------------------------------------------------------
    # /start — top-level menu

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if update.message is None or user is None:
            return
        if not self._is_paired(user.id):
            await update.message.reply_text(
                "You are not paired with this bot.\n\n"
                "Ask the admin for a pairing code, then send /pair <code>."
            )
            return
        if self._has_running_task(user.id):
            await update.message.reply_text(
                "A task is already running. Use /abort first or wait for it to finish."
            )
            return

        sess = self._sessions.get(user.id)
        sess.state = SessionState.CHOOSING_APP
        sess.app_id = None
        sess.task_id = None

        sent = await update.message.reply_text(
            "What do you want to do? Pick an app or type a task in plain English.",
            reply_markup=_app_keyboard(),
        )
        sess.menu_message_id = sent.message_id

    # ------------------------------------------------------------------
    # Free-text message handler

    async def message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if update.message is None or user is None:
            return
        if not self._is_paired(user.id):
            await update.message.reply_text(
                "You are not paired. Use /pair <code> first."
            )
            return

        text = (update.message.text or "").strip()
        if not text:
            await update.message.reply_text("Please send a non-empty task description.")
            return

        sess = self._sessions.get(user.id)
        if sess.state == SessionState.AWAITING_PARAM:
            # User is filling in a templated task's free-text slot.
            await self._launch_templated_task(update, user.id, text)
            return

        if sess.state == SessionState.AWAITING_SAVE_NAME:
            # User is naming a "save as quick task".
            sess.state = SessionState.IDLE
            msg = await self._save_last_run_core(user.id, text)
            await update.message.reply_text(msg)
            return

        if self._has_running_task(user.id):
            await update.message.reply_text(
                "A task is already running. Use /abort first or wait for it to finish."
            )
            return

        # Classifier first (when wired): it recognizes a cross-app COMPARISON
        # ("cheapest pizza on swiggy or zomato") and otherwise resolves a
        # single-app route (delegating to the router internally). This is the
        # daily-driver path: one Telegram message, no taps.
        if self._classifier is not None:
            intent = await self._classifier.classify(text)
            if isinstance(intent, CompareIntent):
                await self._launch_comparison(update.message, user.id, intent)
                return
            if isinstance(intent, ScheduleIntent):
                await update.message.reply_text(
                    await self._create_schedule_core(user.id, intent)
                )
                return
            if isinstance(intent, SaveIntent):
                await update.message.reply_text(
                    await self._save_last_run_core(user.id, intent.name)
                )
                return
            if isinstance(intent, RunSavedIntent):
                row = await self._resolve_saved(user.id, intent.name)
                if row is None:
                    await update.message.reply_text(
                        f'No saved task matches "{intent.name}". See /saved.'
                    )
                    return
                await self._run_saved(update.message, user.id, row)
                return
            if isinstance(intent, SingleIntent):
                await self._launch_route(update, user.id, intent.route)
                return
            await self._launch_freeform_task(update, user.id, text)
            return

        # No classifier wired — legacy router-only path. If the text maps to a
        # known (app, task) pair, execute it; else fall back to freeform.
        if self._router is not None:
            route = await self._router.route(text)
            if route is not None:
                await self._launch_route(update, user.id, route)
                return
        await self._launch_freeform_task(update, user.id, text)

    # ------------------------------------------------------------------
    # Callback handler — menu navigation + HITL approve/deny

    async def menu_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        query = update.callback_query
        if query is None or query.from_user is None:
            return
        user_id = query.from_user.id
        if not self._is_paired(user_id):
            await query.answer("Not authorized.")
            return

        data = query.data or ""
        # Approval callbacks (HITL) are routed to the original flow.
        if data in ("approve", "deny"):
            await self._handle_approval_callback(query, user_id, data)
            return

        await query.answer()

        if data.startswith("app:"):
            await self._on_app_pick(query, user_id, data[4:])
        elif data.startswith("task:"):
            await self._on_task_pick(query, user_id, data[5:])
        elif data.startswith("cmp:"):
            await self._on_comparison_pick(query, user_id, data[4:])
        elif data == "save:last":
            await self._on_save_offer_click(query, user_id)
        elif data == "back:apps":
            await self._on_back_to_apps(query, user_id)
        elif data == "freeform":
            await self._on_freeform_pick(query, user_id)
        elif data == "cancel":
            await self._on_cancel(query, user_id)
        else:
            # Unknown callback — likely from an old message after a redeploy.
            pass

    # ------------------------------------------------------------------
    # Menu transitions

    async def _on_app_pick(self, query, user_id: int, app_id: str) -> None:
        app = get_app(app_id)
        if app is None:
            await query.edit_message_text("That app is no longer available.")
            return
        sess = self._sessions.get(user_id)
        sess.state = SessionState.CHOOSING_TASK
        sess.app_id = app_id
        sess.task_id = None
        await query.edit_message_text(
            f"{app.emoji} {app.name} — what would you like to do?",
            reply_markup=_task_keyboard(app),
        )

    async def _on_task_pick(self, query, user_id: int, task_id: str) -> None:
        sess = self._sessions.get(user_id)
        if sess.app_id is None:
            await query.edit_message_text("Session expired — /start again.")
            return
        app = get_app(sess.app_id)
        if app is None:
            await query.edit_message_text("That app is no longer available.")
            return
        task = get_task(app, task_id)
        if task is None:
            await query.edit_message_text("That task is no longer available.")
            return
        sess.task_id = task_id

        if task.needs_param:
            sess.state = SessionState.AWAITING_PARAM
            await query.edit_message_text(
                f"{app.emoji} {app.name} → {task.label}\n\n{task.param_prompt}"
            )
            return

        # No param needed — fire the task immediately.
        await query.edit_message_text(
            f"Starting: {task.label} in {app.name}."
        )
        await self._launch_task_from_session(query.message, user_id, param=None)

    async def _on_back_to_apps(self, query, user_id: int) -> None:
        sess = self._sessions.get(user_id)
        sess.state = SessionState.CHOOSING_APP
        sess.app_id = None
        sess.task_id = None
        await query.edit_message_text(
            "What do you want to do? Pick an app or type a task in plain English.",
            reply_markup=_app_keyboard(),
        )

    async def _on_freeform_pick(self, query, user_id: int) -> None:
        sess = self._sessions.get(user_id)
        sess.state = SessionState.IDLE
        sess.app_id = None
        sess.task_id = None
        await query.edit_message_text(
            "Tell me what you'd like to do — plain English. I'll figure out the app and the steps.\n\n"
            "Example: 'open the search bar in YouTube and search for jazz'."
        )

    async def _on_cancel(self, query, user_id: int) -> None:
        self._sessions.reset(user_id)
        await query.edit_message_text("Cancelled.")

    # ------------------------------------------------------------------
    # Task hand-off

    async def _launch_templated_task(
        self, update: Update, user_id: int, param: str
    ) -> None:
        if update.message is None:
            return
        if self._has_running_task(user_id):
            await update.message.reply_text(
                "A task is already running. Use /abort first or wait for it to finish."
            )
            return
        await self._launch_task_from_session(update.message, user_id, param=param)

    async def _launch_task_from_session(
        self, message, user_id: int, *, param: str | None
    ) -> None:
        sess = self._sessions.get(user_id)
        if sess.app_id is None or sess.task_id is None:
            await message.reply_text("Session expired — /start again.")
            return
        app = get_app(sess.app_id)
        task_tpl = get_task(app, sess.task_id) if app else None
        if app is None or task_tpl is None:
            await message.reply_text("Session expired — /start again.")
            return

        description = render_prompt(task_tpl.template, param)
        task = Task(user_id=user_id, description=description)
        sess.state = SessionState.RUNNING
        await message.reply_text(
            f"Starting in {app.emoji} {app.name}: {description}"
        )
        self._spawn_task(
            user_id, task, launch_package=app.package,
            last_run=_LastRun(
                description=description, launch_package=app.package,
                app_id=app.id, task_id=task_tpl.id, param=param,
            ),
        )

    async def _launch_freeform_task(
        self, update: Update, user_id: int, text: str
    ) -> None:
        if update.message is None:
            return
        task = Task(user_id=user_id, description=text)
        sess = self._sessions.get(user_id)
        sess.state = SessionState.RUNNING
        sess.app_id = None
        sess.task_id = None
        await update.message.reply_text(f"Starting task: {text}")
        # No launch_package — the agent decides where to start.
        self._spawn_task(
            user_id, task, launch_package=None,
            last_run=_LastRun(description=text),
        )

    async def _launch_route(
        self, update: Update, user_id: int, route: Route
    ) -> None:
        """Execute a structured route from the natural-language router.

        If the route's task needs a param and the router extracted one, run
        immediately. If it needs a param and one wasn't extracted, ask the
        user (AWAITING_PARAM state, same as the menu path).
        """
        if update.message is None:
            return
        sess = self._sessions.get(user_id)
        sess.app_id = route.app.id
        sess.task_id = route.task.id

        if route.task.needs_param and not route.param:
            sess.state = SessionState.AWAITING_PARAM
            await update.message.reply_text(
                f"{route.app.emoji} {route.app.name} → {route.task.label}\n\n"
                f"{route.task.param_prompt}"
            )
            return

        # Have everything we need — execute.
        description = render_prompt(route.task.template, route.param)
        task = Task(user_id=user_id, description=description)
        sess.state = SessionState.RUNNING
        await update.message.reply_text(
            f"{route.app.emoji} {route.app.name}: {description}"
        )
        self._spawn_task(
            user_id, task, launch_package=route.app.package,
            last_run=_LastRun(
                description=description, launch_package=route.app.package,
                app_id=route.app.id, task_id=route.task.id, param=route.param,
            ),
        )

    # ------------------------------------------------------------------
    # Saved quick tasks (feature #4)

    def _spawn_task(
        self,
        user_id: int,
        task: Task,
        *,
        launch_package: str | None = None,
        last_run: "_LastRun | None" = None,
        offer_save: bool = True,
        approval_timeout: float | None = None,
    ) -> asyncio.Task:
        """Launch a single-app task, remember it, and (on success) offer to save.

        Centralizes the create_task + _running bookkeeping so every single-app
        launch captures `_last_run` (for "save this") and, unless suppressed,
        attaches a done-callback that offers a one-tap save when the run
        finishes successfully. `approval_timeout` bounds the HITL wait for
        unattended scheduled runs (None = wait indefinitely, the interactive
        default).
        """
        fut = asyncio.create_task(
            self._orch.run_task(
                task, launch_package=launch_package,
                approval_timeout=approval_timeout,
            )
        )
        self._running[user_id] = fut
        if last_run is not None:
            self._last_run[user_id] = last_run
        if offer_save and self._saved is not None and last_run is not None:
            fut.add_done_callback(
                lambda f, uid=user_id: self._on_run_done(f, uid)
            )
        return fut

    def _on_run_done(self, fut: asyncio.Task, user_id: int) -> None:
        """Done-callback: offer to save a task that completed successfully.

        Sync (asyncio callback contract); schedules the async offer on the loop.
        Swallows cancellation/errors — the orchestrator already reported them.
        """
        if fut.cancelled():
            return
        try:
            task = fut.result()
        except Exception:
            return
        if task is None or getattr(task, "state", None) is not TaskState.DONE:
            return
        if user_id not in self._last_run:
            return
        asyncio.create_task(self._offer_save(user_id))

    async def _offer_save(self, user_id: int) -> None:
        try:
            kb = InlineKeyboardMarkup(
                [[InlineKeyboardButton(
                    "💾 Save as quick task", callback_data="save:last"
                )]]
            )
            await self._app.bot.send_message(
                chat_id=user_id,
                text="Want to save this as a quick task you can re-run anytime?",
                reply_markup=kb,
            )
        except Exception:
            pass

    async def _on_save_offer_click(self, query, user_id: int) -> None:
        if self._saved is None or user_id not in self._last_run:
            await query.edit_message_text("Nothing recent to save.")
            return
        self._sessions.get(user_id).state = SessionState.AWAITING_SAVE_NAME
        await query.edit_message_text(
            "What should I call this quick task? Send me a short name."
        )

    async def _save_last_run_core(self, user_id: int, name: str) -> str:
        """Save the user's last run under `name`; returns a user-facing line."""
        if self._saved is None:
            return "Saving quick tasks isn't enabled."
        last = self._last_run.get(user_id)
        if last is None:
            return "I don't have a recent task to save — run something first."
        label = name.strip()
        slug = _slugify(label)
        if not slug:
            return "That name won't work — use some letters or numbers."
        await self._saved.upsert(
            user_id=user_id, slug=slug, label=label,
            raw_description=last.description, app_id=last.app_id,
            task_id=last.task_id, param=last.param,
            launch_package=last.launch_package,
        )
        return f'Saved as "{label}". Re-run it with /run {slug} or "run my {label}".'

    async def _resolve_saved(
        self, user_id: int, query: str
    ) -> SavedTaskRow | None:
        """Resolve a saved task by name: slug -> exact label -> unique
        substring -> fuzzy. Returns None if nothing matches confidently."""
        if self._saved is None:
            return None
        rows = await self._saved.list_for(user_id)
        if not rows:
            return None
        q = query.strip().lower()
        qslug = _slugify(query)
        for r in rows:
            if r.slug == qslug:
                return r
        for r in rows:
            if r.label.lower() == q:
                return r
        subs = [r for r in rows if q and q in r.label.lower()]
        if len(subs) == 1:
            return subs[0]
        import difflib

        labels = {r.label.lower(): r for r in rows}
        match = difflib.get_close_matches(q, list(labels), n=1, cutoff=0.6)
        return labels[match[0]] if match else None

    async def _run_saved(self, message, user_id: int, row: SavedTaskRow) -> None:
        if self._has_running_task(user_id):
            await message.reply_text("A task is already running. Use /abort first.")
            return
        app = get_app(row.app_id) if row.app_id else None
        task_tpl = get_task(app, row.task_id) if (app and row.task_id) else None
        if app is not None and task_tpl is not None:
            # Structured replay — re-render so template fixes propagate.
            description = render_prompt(task_tpl.template, row.param)
            launch_package = app.package
        else:
            description = row.raw_description
            launch_package = row.launch_package
        if self._saved is not None:
            try:
                await self._saved.mark_run(user_id, row.slug)
            except Exception:
                pass
        task = Task(user_id=user_id, description=description)
        sess = self._sessions.get(user_id)
        sess.state = SessionState.RUNNING
        sess.app_id = row.app_id
        sess.task_id = row.task_id
        await message.reply_text(f'▶️ Running "{row.label}": {description}')
        self._spawn_task(
            user_id, task, launch_package=launch_package,
            last_run=_LastRun(
                description=description, launch_package=launch_package,
                app_id=row.app_id, task_id=row.task_id, param=row.param,
            ),
            offer_save=False,  # already saved
        )

    # ------------------------------------------------------------------
    # Scheduled / recurring tasks (feature #5)

    async def _notify(self, user_id: int, text: str) -> None:
        """Best-effort Telegram message (used by background flows)."""
        try:
            await self._app.bot.send_message(chat_id=user_id, text=text)
        except Exception:
            pass

    async def launch_scheduled(self, row: ScheduleRow) -> bool:
        """Run a due scheduled task — the Scheduler's launcher callback.

        Inherits the one-task-per-user guard: if a task is already running the
        fire is SKIPPED and noted, never queued (single device; a stale order
        could be wrong). Honors the schedule's pay_automatically flag and a
        bounded HITL window so an unattended run can't hang or pay without
        authorization.
        """
        user_id = row.user_id
        if not self._is_paired(user_id):
            return False
        if self._has_running_task(user_id):
            await self._notify(
                user_id,
                f'⏰ Skipped scheduled "{row.name}" — another task is running.',
            )
            return False
        app = get_app(row.app_id) if row.app_id else None
        task_tpl = get_task(app, row.task_id) if (app and row.task_id) else None
        if app is not None and task_tpl is not None:
            description = render_prompt(task_tpl.template, row.param)
            launch_package = app.package
        else:
            description = row.raw_description
            launch_package = row.launch_package
        task = Task(user_id=user_id, description=description)
        if row.pay_automatically:
            task.auto_approve_payment = True
        sess = self._sessions.get(user_id)
        sess.state = SessionState.RUNNING
        sess.app_id = row.app_id
        sess.task_id = row.task_id
        mode = "auto-pay" if row.pay_automatically else "stops at cart"
        await self._notify(
            user_id, f'⏰ Running scheduled "{row.name}" ({mode}): {description}'
        )
        self._spawn_task(
            user_id, task, launch_package=launch_package,
            last_run=_LastRun(
                description=description, launch_package=launch_package,
                app_id=row.app_id, task_id=row.task_id, param=row.param,
            ),
            offer_save=False,
            approval_timeout=_SCHEDULED_APPROVAL_TIMEOUT_SECONDS,
        )
        return True

    async def _create_schedule_core(
        self, user_id: int, intent: ScheduleIntent
    ) -> str:
        """Validate + persist a schedule from a ScheduleIntent; return a line."""
        if self._schedules is None:
            return "Scheduling isn't enabled."
        spec = build_schedule_spec(
            freq=intent.freq, time_str=intent.time_str, tz=self._tz,
            now=datetime.now(timezone.utc),
            weekday_name=intent.weekday_name, day_of_month=intent.day_of_month,
        )
        if spec is None:
            return (
                "I couldn't understand that schedule. Try e.g. "
                '"order milk on blinkit every day at 9am".'
            )
        route = intent.route
        description = render_prompt(route.task.template, route.param)
        name = intent.name or f"{route.app.name}: {route.task.label}"
        sid = await self._schedules.insert(
            user_id=user_id, name=name, freq=intent.freq,
            at_minute=spec["at_minute"], tz=self._tz,
            raw_description=description, next_run_at=spec["next_run_at"],
            weekday=spec["weekday"], day_of_month=spec["day_of_month"],
            app_id=route.app.id, task_id=route.task.id, param=route.param,
            launch_package=route.app.package,
            pay_automatically=intent.pay_automatically,
        )
        phrase = describe_schedule(
            intent.freq, spec["at_minute"], spec["weekday"], spec["day_of_month"]
        )
        pay = (
            " It will pay automatically."
            if intent.pay_automatically
            else " It will stop at the cart for your approval."
        )
        return (
            f'📅 Scheduled "{name}" {phrase} (#{sid}). '
            f"Next run: {_fmt_local(spec['next_run_at'], self._tz)}.{pay}"
        )

    # ------------------------------------------------------------------
    # Cross-app comparison (feature #6)

    async def _launch_comparison(
        self, message, user_id: int, intent: CompareIntent
    ) -> None:
        """Kick off a comparison as the user's single running task."""
        if self._comparison is None:
            await message.reply_text(
                "Cross-app comparison isn't available right now."
            )
            return
        if self._has_running_task(user_id):
            await message.reply_text(
                "A task is already running. Use /abort first or wait for it to finish."
            )
            return
        names = ", ".join(a.name for a in intent.candidates)
        sess = self._sessions.get(user_id)
        sess.state = SessionState.RUNNING
        sess.app_id = None
        sess.task_id = None
        await message.reply_text(
            f"{_ranking_label(intent.ranking_key.value)} '{intent.item}' across "
            f"{names} — checking each app one at a time, give it a moment…"
        )
        self._running[user_id] = asyncio.create_task(
            self._run_comparison(user_id, intent)
        )

    async def _run_comparison(self, user_id: int, intent: CompareIntent) -> None:
        """Run the probes, persist, present the ranked table + order buttons.

        Runs as the user's `_running` task, so /abort cancels it cleanly. Status
        and the final table go to the user's Telegram chat (chat_id == user_id).
        """
        chat_id = user_id

        async def _progress(msg: str) -> None:
            try:
                await self._app.bot.send_message(chat_id=chat_id, text=msg)
            except Exception:
                pass

        try:
            targets = [
                ProbeTarget(a.id, a.name, a.package) for a in intent.candidates
            ]
            result = await self._comparison.run(
                targets=targets,
                item=intent.item,
                category=intent.category,
                ranking_key=intent.ranking_key.value,
                user_id=user_id,
                on_progress=_progress,
            )
            comparison_id: int | None = None
            if self._repo is not None:
                try:
                    comparison_id = await self._repo.insert_comparison(
                        user_id=user_id,
                        query=intent.item,
                        category=intent.category,
                        ranking_key=intent.ranking_key.value,
                        quotes=result.quotes_as_dicts(),
                        winner_app_id=(
                            result.winner.app_id if result.winner else None
                        ),
                    )
                except Exception:
                    comparison_id = None
            text, markup = _render_comparison(result)
            await self._app.bot.send_message(
                chat_id=chat_id, text=text, reply_markup=markup
            )
            self._pending_comparison[user_id] = _PendingComparison(
                result=result,
                comparison_id=comparison_id,
                created_at=time.monotonic(),
            )
            # Voice convenience: stage the winner's order so a spoken "yes"
            # (handle_external_confirm) can order it without the buttons.
            if result.winner is not None:
                app = get_app(result.winner.app_id)
                tpl = _primary_order_task(app) if app is not None else None
                if app is not None and tpl is not None:
                    self._pending[user_id] = _PendingIntent(
                        description=render_prompt(tpl.template, intent.item),
                        launch_package=app.package,
                        created_at=time.monotonic(),
                    )
        except asyncio.CancelledError:
            raise
        except Exception as e:  # pragma: no cover - defensive
            try:
                await self._app.bot.send_message(
                    chat_id=chat_id, text=f"Comparison failed: {e}"
                )
            except Exception:
                pass
        finally:
            sess = self._sessions.get(user_id)
            if sess.state == SessionState.RUNNING:
                sess.state = SessionState.IDLE

    async def _on_comparison_pick(self, query, user_id: int, app_id: str) -> None:
        """Handle a `cmp:<app_id>` button — order from the chosen app."""
        pending = self._pending_comparison.pop(user_id, None)
        if pending is None or (
            time.monotonic() - pending.created_at > _COMPARISON_TTL_SECONDS
        ):
            await query.edit_message_text(
                "That comparison has expired — send it again for fresh prices."
            )
            return
        if app_id == "none":
            await query.edit_message_text("Okay — no order placed.")
            return
        app = get_app(app_id)
        if app is None:
            await query.edit_message_text("That app is no longer available.")
            return
        if self._has_running_task(user_id):
            await query.edit_message_text(
                "A task is already running. Use /abort first."
            )
            return
        tpl = _primary_order_task(app)
        if tpl is None:
            await query.edit_message_text(
                f"I don't have an order action for {app.name}."
            )
            return
        if self._repo is not None and pending.comparison_id is not None:
            try:
                await self._repo.set_comparison_chosen(pending.comparison_id, app_id)
            except Exception:
                pass
        # Hand off to the real single-app order flow: full shopping guards +
        # payment/OTP HITL are armed here (read_only defaults False).
        description = render_prompt(tpl.template, pending.result.item)
        task = Task(user_id=user_id, description=description)
        sess = self._sessions.get(user_id)
        sess.state = SessionState.RUNNING
        sess.app_id = app.id
        sess.task_id = tpl.id
        await query.edit_message_text(f"{app.emoji} {app.name}: {description}")
        self._running[user_id] = asyncio.create_task(
            self._orch.run_task(task, launch_package=app.package)
        )

    # ------------------------------------------------------------------
    # External trigger (Android voice / widget -> webhook)
    #
    # Two phases so the user hears what was understood before anything runs:
    #   handle_external_trigger -> parse + stash a pending intent, speak it back
    #   handle_external_confirm -> launch it (yes) or drop it (no)
    # Either way, status / HITL approval / payment+OTP gates / final result all
    # still flow to the owner's Telegram chat — voice only confirms the intent.

    async def handle_external_trigger(self, user_id: int, text: str) -> str:
        """Phase 1: parse the spoken text and propose it, without launching.

        Stores a pending intent and returns a short line to be spoken back so
        the user can catch a mis-hear or wrong-app before the agent acts. The
        caller must then call handle_external_confirm. Returns never leak task
        internals.
        """
        if not self._is_paired(user_id):
            return "That user isn't paired with the bot."
        text = text.strip()
        if not text:
            return "I didn't catch a task — try again."
        # Strip the wake word the phone-side client prefixes ("atlas order milk"
        # -> "order milk") so the router scores the actual command. Falls
        # through unchanged when the wake word isn't present, e.g. for the
        # widget/Test Command paths which already POST a clean phrase.
        text = _strip_wake_word(text)
        if not text:
            return "I didn't catch a task — try again."
        # Echo the recognized command to Telegram so the owner sees exactly what
        # the voice trigger heard — visualization for voice-driven runs. This is
        # non-critical chrome: a failed echo must never abort the trigger.
        try:
            await self._app.bot.send_message(
                chat_id=user_id, text=f'🎙️ Heard: "{text}"'
            )
        except Exception:
            pass
        if self._has_running_task(user_id):
            return "A task is already running. Finish or abort it first."

        description = text
        launch_package: str | None = None
        spoken = text

        # Resolve the intent. With the classifier wired we also recognize a
        # cross-app comparison; otherwise fall back to the single-app router.
        route: Route | None = None
        if self._classifier is not None:
            intent = await self._classifier.classify(text)
            if isinstance(intent, CompareIntent):
                # A comparison runs several probes (tens of seconds each) — far
                # too long to finish inside this trigger call. Stage it; on a
                # spoken "yes" it launches and posts the result to Telegram.
                names = ", ".join(a.name for a in intent.candidates)
                self._pending[user_id] = _PendingIntent(
                    description=f"compare {intent.item}",
                    launch_package=None,
                    created_at=time.monotonic(),
                    compare=intent,
                )
                return (
                    f"Got it — compare {intent.item} on {names}. "
                    "I'll show the result on Telegram. Confirm?"
                )
            if isinstance(intent, ScheduleIntent):
                # Creating a schedule touches no device state — do it now.
                return await self._create_schedule_core(user_id, intent)
            if isinstance(intent, SaveIntent):
                # Saving touches no device state — do it immediately, no confirm.
                return await self._save_last_run_core(user_id, intent.name)
            if isinstance(intent, RunSavedIntent):
                row = await self._resolve_saved(user_id, intent.name)
                if row is None:
                    return f"I couldn't find a saved task called {intent.name}."
                app = get_app(row.app_id) if row.app_id else None
                task_tpl = (
                    get_task(app, row.task_id) if (app and row.task_id) else None
                )
                if app is not None and task_tpl is not None:
                    description = render_prompt(task_tpl.template, row.param)
                    launch_package = app.package
                else:
                    description = row.raw_description
                    launch_package = row.launch_package
                self._pending[user_id] = _PendingIntent(
                    description=description,
                    launch_package=launch_package,
                    created_at=time.monotonic(),
                )
                return f"Got it — run {row.label}. Confirm?"
            if isinstance(intent, SingleIntent):
                route = intent.route
        elif self._router is not None:
            route = await self._router.route(text)

        if route is not None:
            if route.task.needs_param and not route.param:
                # A missing slot can't be gathered over voice (we cancel on
                # "no", no re-listen) — defer to the Telegram param prompt.
                sess = self._sessions.get(user_id)
                sess.app_id = route.app.id
                sess.task_id = route.task.id
                sess.state = SessionState.AWAITING_PARAM
                await self._app.bot.send_message(
                    chat_id=user_id,
                    text=(
                        f"{route.app.emoji} {route.app.name} → {route.task.label}\n\n"
                        f"{route.task.param_prompt}"
                    ),
                )
                return f"{route.app.name} needs a detail — check Telegram to continue."
            description = render_prompt(route.task.template, route.param)
            launch_package = route.app.package
            spoken = f"{route.app.name}: {description}"

        self._pending[user_id] = _PendingIntent(
            description=description,
            launch_package=launch_package,
            created_at=time.monotonic(),
        )
        # The literal "Confirm?" is a contract with the Android HTTP-Shortcuts
        # client: its on-success script does `data.message.includes("Confirm?")`
        # to decide whether to prompt for a spoken yes/no and re-trigger with a
        # {"confirm": ...} payload. Keep that token in this (and only this)
        # proposal message — confirm/cancel/error replies must NOT contain it,
        # or the phone would loop asking to confirm a confirmation.
        return f"Got it — {spoken}. Confirm?"

    async def handle_external_run(self, user_id: int, text: str) -> str:
        """Single-shot: propose AND launch in one call, no confirm round-trip.

        For trigger clients that can't reliably show a confirmation prompt
        (some Android HTTP-Shortcuts builds have a broken `prompt()`/`tts()`),
        the two-phase voice confirm is unusable. This collapses it: parse +
        launch immediately, returning the launch message. Safety is
        unchanged — payment/OTP/delete still gate on Telegram, and a
        mis-heard product is caught at the cart-review HITL.

        Reuses the proposal path so routing, brand matching, and the
        param-needed deferral all behave identically; it only auto-confirms
        when a pending intent was actually created (i.e. not the
        param-needed / error cases, which return their own guidance).
        """
        msg = await self.handle_external_trigger(user_id, text)
        if user_id in self._pending:
            return await self.handle_external_confirm(user_id, True)
        return msg

    async def handle_external_confirm(self, user_id: int, approve: bool) -> str:
        """Phase 2: act on the spoken yes/no for the pending proposal."""
        pending = self._pending.pop(user_id, None)
        if pending is None or (
            time.monotonic() - pending.created_at > _PENDING_TTL_SECONDS
        ):
            return "There's nothing to confirm — say the task first."
        if not approve:
            return "Okay, cancelled."
        if self._has_running_task(user_id):
            return "A task is already running. Finish or abort it first."

        # A staged comparison: launch the multi-app probe flow instead of a
        # single task. Results + order buttons land on Telegram.
        if pending.compare is not None:
            sess = self._sessions.get(user_id)
            sess.state = SessionState.RUNNING
            sess.app_id = None
            sess.task_id = None
            self._running[user_id] = asyncio.create_task(
                self._run_comparison(user_id, pending.compare)
            )
            return "On it — comparing now; I'll post the result on Telegram."

        sess = self._sessions.get(user_id)
        sess.state = SessionState.RUNNING
        sess.app_id = None
        sess.task_id = None
        task = Task(user_id=user_id, description=pending.description)
        await self._app.bot.send_message(
            chat_id=user_id, text=f"Starting: {pending.description}"
        )
        self._spawn_task(
            user_id, task, launch_package=pending.launch_package,
            last_run=_LastRun(
                description=pending.description,
                launch_package=pending.launch_package,
            ),
        )
        return "On it — I'll confirm on Telegram."

    # ------------------------------------------------------------------
    # Other commands (unchanged from before)

    # ------------------------------------------------------------------
    # Saved-task commands (feature #4)

    async def save_task(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if update.message is None or user is None:
            return
        if not self._is_paired(user.id):
            return
        name = " ".join(context.args or []).strip()
        if not name:
            await update.message.reply_text(
                "Usage: /save <name> — saves the task you just ran."
            )
            return
        await update.message.reply_text(
            await self._save_last_run_core(user.id, name)
        )

    async def list_saved(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if update.message is None or user is None:
            return
        if not self._is_paired(user.id):
            return
        if self._saved is None:
            await update.message.reply_text("Saved tasks aren't enabled.")
            return
        rows = await self._saved.list_for(user.id)
        if not rows:
            await update.message.reply_text(
                "No saved tasks yet. Run something, then tap “Save as quick task”."
            )
            return
        lines = ["Your saved tasks:"]
        for r in rows:
            head = r.raw_description[:50].replace("\n", " ")
            if len(r.raw_description) > 50:
                head += "…"
            lines.append(f"• {r.label}  —  /run {r.slug}\n    {head}")
        await update.message.reply_text("\n".join(lines))

    async def run_saved(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if update.message is None or user is None:
            return
        if not self._is_paired(user.id):
            return
        name = " ".join(context.args or []).strip()
        if not name:
            await update.message.reply_text("Usage: /run <name>  (see /saved)")
            return
        row = await self._resolve_saved(user.id, name)
        if row is None:
            await update.message.reply_text(
                f'No saved task matches "{name}". See /saved.'
            )
            return
        await self._run_saved(update.message, user.id, row)

    async def forget_saved(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if update.message is None or user is None:
            return
        if not self._is_paired(user.id):
            return
        name = " ".join(context.args or []).strip()
        if not name or self._saved is None:
            await update.message.reply_text("Usage: /forget <name>")
            return
        row = await self._resolve_saved(user.id, name)
        if row is None:
            await update.message.reply_text(f'No saved task matches "{name}".')
            return
        await self._saved.delete(user.id, row.slug)
        await update.message.reply_text(f'Forgot "{row.label}".')

    # ------------------------------------------------------------------
    # Schedule commands (feature #5)

    async def schedule_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if update.message is None or user is None:
            return
        if not self._is_paired(user.id):
            return
        if self._classifier is None or self._schedules is None:
            await update.message.reply_text("Scheduling isn't enabled.")
            return
        text = " ".join(context.args or []).strip()
        if not text:
            await update.message.reply_text(
                "Usage: /schedule <task> <when>\n"
                "E.g. /schedule order milk on blinkit every day at 9am"
            )
            return
        intent = await self._classifier.classify(text)
        if not isinstance(intent, ScheduleIntent):
            await update.message.reply_text(
                "I couldn't parse a schedule from that. Include a task and a "
                'time, e.g. "order milk on blinkit every day at 9am".'
            )
            return
        await update.message.reply_text(
            await self._create_schedule_core(user.id, intent)
        )

    async def list_schedules(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if update.message is None or user is None:
            return
        if not self._is_paired(user.id):
            return
        if self._schedules is None:
            await update.message.reply_text("Scheduling isn't enabled.")
            return
        rows = await self._schedules.list_for(user.id)
        if not rows:
            await update.message.reply_text(
                "No schedules yet. Create one with /schedule or just say "
                '"order milk on blinkit every day at 9am".'
            )
            return
        lines = ["Your schedules:"]
        for r in rows:
            phrase = describe_schedule(r.freq, r.at_minute, r.weekday, r.day_of_month)
            off = "" if r.enabled else " (off)"
            pay = " · auto-pay" if r.pay_automatically else ""
            lines.append(
                f"#{r.id} {r.name} — {phrase}{pay}{off}\n"
                f"    next: {_fmt_local(r.next_run_at, self._tz)}  ·  /unschedule {r.id}"
            )
        await update.message.reply_text("\n".join(lines))

    async def unschedule(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if update.message is None or user is None:
            return
        if not self._is_paired(user.id) or self._schedules is None:
            return
        args = context.args or []
        if not args:
            await update.message.reply_text("Usage: /unschedule <id> (see /schedules)")
            return
        try:
            sid = int(args[0])
        except ValueError:
            await update.message.reply_text("The id must be a number — see /schedules.")
            return
        removed = await self._schedules.delete(user.id, sid)
        await update.message.reply_text(
            f"Removed schedule #{sid}." if removed else f"No schedule #{sid}."
        )

    async def status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if update.message is None or user is None:
            return
        if not self._is_paired(user.id):
            return
        task = self._orch.get_task(user.id)
        if task is None:
            await update.message.reply_text("No task in progress.")
            return
        rpd_str = (
            f"{task.latest_rpd_remaining}"
            if task.latest_rpd_remaining is not None
            else "?"
        )
        await update.message.reply_text(
            f"State: {task.state.value}\n"
            f"Step: {task.step_count}\n"
            f"Task: {task.description}\n"
            f"Tokens: {task.total_input_tokens} in / {task.total_output_tokens} out\n"
            f"RPD remaining: {rpd_str}"
        )

    async def history(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if update.message is None or user is None:
            return
        if not self._is_paired(user.id):
            return
        if self._repo is None:
            await update.message.reply_text("History is not enabled.")
            return
        rows = await self._repo.list_recent(user.id, limit=10)
        if not rows:
            await update.message.reply_text("No past tasks.")
            return
        lines: list[str] = []
        for r in rows:
            head = r.description[:60].replace("\n", " ")
            if len(r.description) > 60:
                head += "..."
            dur = r.duration_seconds()
            dur_str = f"{dur:.0f}s" if dur is not None else "running"
            lines.append(f"[{r.state}] {head} ({dur_str})")
        await update.message.reply_text("\n".join(lines))

    async def abort(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if update.message is None or user is None:
            return
        if not self._is_paired(user.id):
            return
        running = self._running.get(user.id)
        if running is not None and not running.done():
            running.cancel()
            self._sessions.reset(user.id)
            await update.message.reply_text("Task aborted.")
        else:
            await update.message.reply_text("No task to abort.")

    # ------------------------------------------------------------------
    # Pairing flow (unchanged)

    async def issue_pair_code(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        user = update.effective_user
        if update.message is None or user is None:
            return
        if not self._is_admin(user.id):
            await update.message.reply_text("Admin only.")
            return
        hint = " ".join(context.args or []) or ""
        pc = self._pairing.issue(name_hint=hint)
        ttl_min = max(1, int(self._pairing.time_remaining() // 60))
        await update.message.reply_text(
            f"Pair code: `{pc.code}`\nValid for ~{ttl_min} min.\n\n"
            "Have the user send: /pair " + pc.code,
            parse_mode="Markdown",
        )

    async def pair(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if update.message is None or user is None:
            return
        args = context.args or []
        if not args:
            await update.message.reply_text("Usage: /pair <code>")
            return
        if self._users.is_allowed(user.id):
            await update.message.reply_text("You are already paired.")
            return
        pc = self._pairing.redeem(args[0].strip())
        if pc is None:
            await update.message.reply_text("Invalid or expired code.")
            return
        name = pc.name_hint or (user.username or user.first_name or str(user.id))
        record = UserRecord.new(
            user_id=user.id, name=name, policy=UserPolicy.CONFIRM_SENSITIVE
        )
        await self._users.add(record)
        await update.message.reply_text(
            f"Paired as {name}. Default policy: confirm_sensitive.\n"
            "Send /start to pick an app or just type a task."
        )

    async def list_users(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        user = update.effective_user
        if update.message is None or user is None:
            return
        if not self._is_admin(user.id):
            await update.message.reply_text("Admin only.")
            return
        records = self._users.list_all()
        if not records:
            await update.message.reply_text("No paired users.")
            return
        lines = [
            f"{r.user_id} · {r.name} · {r.policy.value} · {r.paired_at}"
            for r in records
        ]
        await update.message.reply_text("\n".join(lines))

    async def revoke(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if update.message is None or user is None:
            return
        if not self._is_admin(user.id):
            await update.message.reply_text("Admin only.")
            return
        args = context.args or []
        if not args:
            await update.message.reply_text("Usage: /revoke <user_id>")
            return
        try:
            target = int(args[0])
        except ValueError:
            await update.message.reply_text("user_id must be an integer.")
            return
        removed = await self._users.remove(target)
        await update.message.reply_text(
            f"Revoked {target}." if removed else f"No such paired user: {target}."
        )

    # ------------------------------------------------------------------
    # HITL approval (existing, unchanged)

    async def _handle_approval_callback(self, query, user_id: int, data: str) -> None:
        await query.answer()
        if data == "approve":
            self._hitl.grant(user_id)
            if query.message:
                await query.edit_message_text("Action approved.")
        elif data == "deny":
            self._hitl.deny(user_id)
            if query.message:
                await query.edit_message_text("Action denied.")

    # Keep the old name registered as the catch-all callback handler.
    approval_callback = menu_callback

    async def on_approval_request(self, task: Task, action: dict) -> None:
        reason = (
            action.get("reason")
            or action.get("summary")
            or f"sensitive action: {action.get('action')}"
        )
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("Approve", callback_data="approve"),
                    InlineKeyboardButton("Deny", callback_data="deny"),
                ]
            ]
        )
        await self._app.bot.send_message(
            chat_id=task.user_id,
            text=(
                "Approval needed before continuing.\n\n"
                f"Reason: {reason}\n"
                f"Action: {action}"
            ),
            reply_markup=keyboard,
        )
        # Mirror the approval to the dashboard (view-only — it shows a banner
        # "respond in Telegram"; it never grants approvals itself).
        if self._event_bus is not None:
            try:
                self._event_bus.publish({
                    "type": "approval",
                    "user_id": task.user_id,
                    "reason": str(reason),
                    "action_type": action.get("action"),
                    "note": "Approve/Deny in Telegram",
                    "ts": datetime.now(timezone.utc).isoformat(),
                })
            except Exception:
                pass

    async def on_status_update(self, task: Task, message: str) -> None:
        await self._app.bot.send_message(
            chat_id=task.user_id,
            text=f"[{task.state.value}] {message}",
        )


# ---------------------------------------------------------------------------
# Cross-app comparison rendering

_RANKING_LABELS = {
    "cheapest": "💰 Comparing cheapest",
    "fastest": "⚡ Comparing fastest",
    "best": "⭐ Comparing best",
}
_RANKING_HEADERS = {"cheapest": "Cheapest", "fastest": "Fastest", "best": "Best"}


def _slugify(name: str) -> str:
    """Lowercase, hyphenated slug for a saved-task name (e.g. 'Sunday order'
    -> 'sunday-order'). Empty when the name has no alphanumerics."""
    return re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")


def _fmt_local(iso_utc: str, tz: str) -> str:
    """Render a UTC ISO timestamp in the user's local tz for confirmations."""
    try:
        return (
            datetime.fromisoformat(iso_utc)
            .astimezone(ZoneInfo(tz))
            .strftime("%a %d %b, %H:%M")
        )
    except Exception:
        return iso_utc


def _ranking_label(ranking_key: str) -> str:
    return _RANKING_LABELS.get(ranking_key, "🔎 Comparing")


def _primary_order_task(app: App) -> TaskTemplate | None:
    """The app's main param-taking action, used to order after a comparison."""
    by_id = {t.id: t for t in app.tasks}
    for tid in _ORDER_TASK_PRIORITY:
        t = by_id.get(tid)
        if t is not None and t.needs_param:
            return t
    for t in app.tasks:
        if t.needs_param and t.id != "free":
            return t
    return None


def _fmt_price(price: float, currency: str) -> str:
    amount = int(price) if float(price).is_integer() else round(price, 2)
    if (currency or "INR").upper() == "INR":
        return f"₹{amount}"
    return f"{currency} {amount}"


def _render_comparison(
    result: ComparisonResult,
) -> tuple[str, InlineKeyboardMarkup | None]:
    """Render a ranked comparison to Telegram text + per-app order buttons."""
    header = _RANKING_HEADERS.get(result.ranking_key, "Comparison")
    lines = [f"{header} for '{result.item}' ({result.category}):", ""]
    for q in result.quotes:
        app = get_app(q.app_id)
        emoji = app.emoji if app else "•"
        if not q.ok:
            lines.append(
                f"{emoji} {q.app_name} — couldn't check "
                f"({q.failure_reason or 'failed'})"
            )
            continue
        if q.available is False:
            lines.append(f"{emoji} {q.app_name} — unavailable")
            continue
        bits: list[str] = []
        if q.price is not None:
            bits.append(_fmt_price(q.price, q.currency))
        if q.eta:
            bits.append(q.eta)
        detail = " · ".join(bits) if bits else "no price found"
        crown = (
            "  🏆"
            if result.winner is not None and q.app_id == result.winner.app_id
            else ""
        )
        lines.append(f"{emoji} {q.app_name} — {detail}{crown}")

    if result.winner is None:
        lines += [
            "",
            "Couldn't get a comparable price from any app. Try again, or open "
            "one directly.",
        ]
        return "\n".join(lines), None

    lines += ["", "Order from which app?"]
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for q in result.ranked:
        app = get_app(q.app_id)
        emoji = app.emoji if app else ""
        tail = (
            _fmt_price(q.price, q.currency)
            if q.price is not None
            else (q.eta or "")
        )
        label = f"{emoji} {q.app_name} {tail}".strip()
        row.append(InlineKeyboardButton(label, callback_data=f"cmp:{q.app_id}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("✖ None", callback_data="cmp:none")])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


# ---------------------------------------------------------------------------
# Inline keyboard builders

def _app_keyboard() -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for app in APPS:
        row.append(
            InlineKeyboardButton(
                f"{app.emoji} {app.name}", callback_data=f"app:{app.id}"
            )
        )
        if len(row) == _APP_BUTTONS_PER_ROW:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append(
        [
            InlineKeyboardButton("📝 Free-form", callback_data="freeform"),
            InlineKeyboardButton("✖ Cancel", callback_data="cancel"),
        ]
    )
    return InlineKeyboardMarkup(rows)


def _task_keyboard(app: App) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for t in app.tasks:
        row.append(InlineKeyboardButton(t.label, callback_data=f"task:{t.id}"))
        if len(row) == _TASK_BUTTONS_PER_ROW:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("⬅ Back", callback_data="back:apps")])
    return InlineKeyboardMarkup(rows)
