"""Telegram callbacks: menu-driven app/task flow + pairing + admin + approval."""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, ContextTypes

from agent.orchestrator import Orchestrator
from agent.persistence import TaskRepository
from agent.state_machine import Task
from bot.apps import (
    APPS,
    App,
    TaskTemplate,
    get_app,
    get_task,
    render_prompt,
)
from bot.pairing import PairCodeIssuer
from bot.router import Route, Router
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


@dataclass
class _PendingIntent:
    """A parsed-but-not-yet-launched task awaiting voice confirmation."""

    description: str
    launch_package: str | None
    created_at: float


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
    ) -> None:
        self._app = application
        self._orch = orchestrator
        self._hitl = hitl
        self._users = users
        self._pairing = pairing
        self._admin_id = admin_id
        self._repo = repo
        self._router = router
        self._sessions = SessionStore()
        self._running: dict[int, asyncio.Task] = {}
        # External-trigger proposals awaiting a spoken yes/no, keyed by user.
        self._pending: dict[int, _PendingIntent] = {}

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

        if self._has_running_task(user.id):
            await update.message.reply_text(
                "A task is already running. Use /abort first or wait for it to finish."
            )
            return

        # Try the router first — if the user's free text maps cleanly to a
        # known (app, task) pair, skip the menu entirely and execute. This is
        # the daily-driver path: one Telegram message, no taps.
        if self._router is not None:
            route = await self._router.route(text)
            if route is not None:
                await self._launch_route(update, user.id, route)
                return

        # Router didn't find a match (or isn't configured) — fall back to the
        # free-form agent path: model figures out everything from scratch.
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
        self._running[user_id] = asyncio.create_task(
            self._orch.run_task(task, launch_package=app.package)
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
        self._running[user_id] = asyncio.create_task(self._orch.run_task(task))

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
        self._running[user_id] = asyncio.create_task(
            self._orch.run_task(task, launch_package=route.app.package)
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
        if self._has_running_task(user_id):
            return "A task is already running. Finish or abort it first."

        description = text
        launch_package: str | None = None
        spoken = text

        if self._router is not None:
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

        sess = self._sessions.get(user_id)
        sess.state = SessionState.RUNNING
        sess.app_id = None
        sess.task_id = None
        task = Task(user_id=user_id, description=pending.description)
        await self._app.bot.send_message(
            chat_id=user_id, text=f"Starting: {pending.description}"
        )
        self._running[user_id] = asyncio.create_task(
            self._orch.run_task(task, launch_package=pending.launch_package)
        )
        return "On it — I'll confirm on Telegram."

    # ------------------------------------------------------------------
    # Other commands (unchanged from before)

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

    async def on_status_update(self, task: Task, message: str) -> None:
        await self._app.bot.send_message(
            chat_id=task.user_id,
            text=f"[{task.state.value}] {message}",
        )


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
