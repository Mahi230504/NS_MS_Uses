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


# Generous network timeouts. On throttled networks (ISP-level Telegram
# throttling is common in some regions) the TLS connect to api.telegram.org
# can take 5-10s — well past python-telegram-bot's 5s default, which makes
# the bootstrap get_me() time out and the whole process abort. 30s absorbs
# that. read timeout must comfortably exceed the long-poll timeout (10s).
_CONNECT_TIMEOUT = 30.0
_READ_TIMEOUT = 30.0
_WRITE_TIMEOUT = 30.0
_POOL_TIMEOUT = 30.0


def build_application(token: str) -> Application:
    return (
        ApplicationBuilder()
        .token(token)
        .connect_timeout(_CONNECT_TIMEOUT)
        .read_timeout(_READ_TIMEOUT)
        .write_timeout(_WRITE_TIMEOUT)
        .pool_timeout(_POOL_TIMEOUT)
        # The long-poll getUpdates calls get their own (also generous) limits
        # so a slow link doesn't kill the polling loop mid-session.
        .get_updates_connect_timeout(_CONNECT_TIMEOUT)
        .get_updates_read_timeout(_READ_TIMEOUT)
        .build()
    )


def register_handlers(app: Application, handlers: Handlers) -> None:
    app.add_handler(CommandHandler("start", handlers.start))
    app.add_handler(CommandHandler("status", handlers.status))
    app.add_handler(CommandHandler("abort", handlers.abort))
    app.add_handler(CommandHandler("history", handlers.history))
    app.add_handler(CommandHandler("save", handlers.save_task))
    app.add_handler(CommandHandler("saved", handlers.list_saved))
    app.add_handler(CommandHandler("run", handlers.run_saved))
    app.add_handler(CommandHandler("forget", handlers.forget_saved))
    app.add_handler(CommandHandler("schedule", handlers.schedule_cmd))
    app.add_handler(CommandHandler("schedules", handlers.list_schedules))
    app.add_handler(CommandHandler("unschedule", handlers.unschedule))
    app.add_handler(CommandHandler("contact", handlers.contact_cmd))
    app.add_handler(CommandHandler("pair", handlers.pair))
    app.add_handler(CommandHandler("issue_pair_code", handlers.issue_pair_code))
    app.add_handler(CommandHandler("users", handlers.list_users))
    app.add_handler(CommandHandler("revoke", handlers.revoke))
    app.add_handler(CallbackQueryHandler(handlers.approval_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.message))
