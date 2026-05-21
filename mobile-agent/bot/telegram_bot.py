"""Bot init, webhook/polling setup."""
from __future__ import annotations

from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from bot.handlers import Handlers


def build_application(token: str) -> Application:
    return ApplicationBuilder().token(token).build()


def register_handlers(app: Application, handlers: Handlers) -> None:
    app.add_handler(CommandHandler("start", handlers.start))
    app.add_handler(CommandHandler("status", handlers.status))
    app.add_handler(CommandHandler("abort", handlers.abort))
    app.add_handler(CommandHandler("history", handlers.history))
    app.add_handler(CommandHandler("pair", handlers.pair))
    app.add_handler(CommandHandler("issue_pair_code", handlers.issue_pair_code))
    app.add_handler(CommandHandler("users", handlers.list_users))
    app.add_handler(CommandHandler("revoke", handlers.revoke))
    app.add_handler(CallbackQueryHandler(handlers.approval_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.message))
