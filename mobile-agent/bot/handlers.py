"""Telegram callbacks: lifecycle, pairing, admin, approval."""
from __future__ import annotations

import asyncio

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, ContextTypes

from agent.orchestrator import Orchestrator
from agent.persistence import TaskRepository
from agent.state_machine import Task
from bot.pairing import PairCodeIssuer
from bot.users import UserPolicy, UserRecord, UserStore
from security.hitl_gate import HitlGate


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
    ) -> None:
        self._app = application
        self._orch = orchestrator
        self._hitl = hitl
        self._users = users
        self._pairing = pairing
        self._admin_id = admin_id
        self._repo = repo
        self._running: dict[int, asyncio.Task] = {}

    def _is_paired(self, user_id: int | None) -> bool:
        return user_id is not None and self._users.is_allowed(user_id)

    def _is_admin(self, user_id: int | None) -> bool:
        return (
            self._admin_id is not None
            and user_id is not None
            and user_id == self._admin_id
        )

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
        await update.message.reply_text(
            "mobile-agent — generalized Android automation.\n\n"
            "Send me a task in plain English (e.g. 'open the search bar in YouTube').\n\n"
            "Commands:\n"
            "  /status — current task state\n"
            "  /history — last 10 tasks\n"
            "  /abort — cancel running task\n"
            "  /pair <code> — finish pairing"
        )

    async def message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if update.message is None or user is None:
            return
        if not self._is_paired(user.id):
            await update.message.reply_text(
                "You are not paired. Use /pair <code> first."
            )
            return

        existing = self._running.get(user.id)
        if existing is not None and not existing.done():
            await update.message.reply_text(
                "A task is already running. Use /abort first or wait for it to finish."
            )
            return

        text = (update.message.text or "").strip()
        if not text:
            await update.message.reply_text("Please send a non-empty task description.")
            return

        task = Task(user_id=user.id, description=text)
        await update.message.reply_text(f"Starting task: {text}")
        self._running[user.id] = asyncio.create_task(self._orch.run_task(task))

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
            await update.message.reply_text("Task aborted.")
        else:
            await update.message.reply_text("No task to abort.")

    # ------------------------------------------------------------------
    # Pairing flow

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
            "Send a task to get started, or /start for help."
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
    # Callbacks the orchestrator pushes back into Telegram.

    async def approval_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        query = update.callback_query
        if query is None or query.from_user is None:
            return
        user_id = query.from_user.id
        if not self._is_paired(user_id):
            await query.answer("Not authorized.")
            return
        await query.answer()
        data = query.data or ""
        if data == "approve":
            self._hitl.grant(user_id)
            if query.message:
                await query.edit_message_text("Action approved.")
        elif data == "deny":
            self._hitl.deny(user_id)
            if query.message:
                await query.edit_message_text("Action denied.")

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
