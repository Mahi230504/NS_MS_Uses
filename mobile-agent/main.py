"""Entry point: wires config, bot, and agent orchestrator."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from agent.orchestrator import Orchestrator
from agent.persistence import TaskRepository
from agent.profiles import resolve_profile
from agent.providers import make_provider
from agent.skills import SkillRegistry
from bot.handlers import Handlers
from bot.pairing import PairCodeIssuer
from bot.router import Router
from bot.telegram_bot import build_application, register_handlers
from bot.users import UserPolicy, UserStore
from config.settings import load_settings
from device.adb_controller import AdbController
from device.emulator import check_emulator_running
from security.audit_logger import AuditLogger
from security.hitl_gate import HitlGate


log = logging.getLogger("mobile_agent.main")


def _provider_kwargs(settings) -> dict:
    """Translate Settings into per-provider constructor kwargs."""
    if settings.vision_provider == "gemini":
        return {"api_key": settings.gemini_api_key, "model": settings.gemini_model}
    if settings.vision_provider == "vertex":
        return {
            "project": settings.gcp_project_id,
            "location": settings.gcp_location,
            "model": settings.gemini_model,
        }
    if settings.vision_provider == "openrouter":
        return {
            "api_key": settings.openrouter_api_key,
            "model": settings.openrouter_model,
        }
    raise RuntimeError(f"Unhandled VISION_PROVIDER: {settings.vision_provider!r}")


async def _verify_emulator(expected_device_id: str, allow_physical: bool) -> None:
    devices = await check_emulator_running(allow_physical=allow_physical)
    if expected_device_id not in devices:
        raise RuntimeError(
            f"Expected device '{expected_device_id}' is not attached. "
            f"Found: {devices}"
        )


def _bootstrap_users(users: UserStore, env_ids: frozenset[int]) -> None:
    """One-time migration from TELEGRAM_ALLOWED_USER_IDS into users.json."""
    if not env_ids:
        return
    if users.list_all():
        return  # users.json already populated — env var is now redundant.
    log.warning(
        "TELEGRAM_ALLOWED_USER_IDS is deprecated. Migrating %d user(s) into %s with "
        "policy 'confirm_sensitive'. Remove the env var once pairing is in place.",
        len(env_ids),
        users.path,
    )
    asyncio.run(users.seed_from_env(env_ids, policy=UserPolicy.CONFIRM_SENSITIVE))


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    settings = load_settings()

    asyncio.run(
        _verify_emulator(settings.android_device_id, settings.allow_physical_device)
    )
    if settings.allow_physical_device:
        log.warning(
            "ALLOW_PHYSICAL_DEVICE is set — driving real phone %s. "
            "HITL gates remain in place; review payment/OTP prompts carefully.",
            settings.android_device_id,
        )

    adb = AdbController(settings.android_device_id)
    # If a previous bot run was killed before its finally-block could
    # restore the IME, the device may still be on ADBKeyboard. Switch off
    # before we start so the user's normal keyboard works while idle.
    try:
        asyncio.run(adb.force_off_adbkeyboard())
    except Exception:
        log.warning("startup IME cleanup failed; user may need to switch in Settings")
    hitl = HitlGate()
    audit = AuditLogger(settings.log_dir)
    vision = make_provider(
        settings.vision_provider,
        **_provider_kwargs(settings),
    )

    users = UserStore(settings.users_path)
    _bootstrap_users(users, settings.telegram_allowed_user_ids_env)

    # If admin is configured and not yet paired, register them with the
    # standard policy. Admin status is a separate axis from HITL policy:
    # admin-only commands (/issue_pair_code, /users, /revoke) check
    # `admin_id`, not policy — so confirm_sensitive doesn't block them from
    # bootstrapping pair codes, but DOES keep the payment/OTP HITL gate in
    # play when they run tasks themselves.
    if (
        settings.telegram_admin_id is not None
        and not users.is_allowed(settings.telegram_admin_id)
    ):
        from bot.users import UserRecord

        asyncio.run(
            users.add(
                UserRecord.new(
                    user_id=settings.telegram_admin_id,
                    name="admin",
                    policy=UserPolicy.CONFIRM_SENSITIVE,
                )
            )
        )

    pairing = PairCodeIssuer()
    skills = SkillRegistry(Path(__file__).resolve().parent / "skills")

    repo = TaskRepository(settings.db_path)
    asyncio.run(repo.initialize())
    recovered = asyncio.run(repo.recover_orphans())
    if recovered:
        log.warning(
            "Marked %d orphan task row(s) as FAILED (previous run did not exit cleanly).",
            recovered,
        )

    orchestrator = Orchestrator(
        adb,
        hitl,
        audit,
        vision,
        settings.session_timeout_seconds,
        users=users,
        skills=skills,
        repo=repo,
        enable_vision_hitl=settings.enable_vision_hitl,
        artifact_dir=settings.log_dir / "screenshots",
        # Activate per-app grounding: commerce packages → COMMERCE profile
        # (shopping validators + [ACTION]/[CART]/... tags), everything else →
        # GENERIC (lean structural-only path). Without this the orchestrator
        # treats every app as commerce (the pre-generalization default).
        profile_resolver=resolve_profile,
    )

    # Intent router — only wire if the provider supports text completion
    # (currently OpenRouter). Other providers fall back to menu-only flow.
    router_instance: Router | None = None
    if hasattr(vision, "complete_text"):
        router_instance = Router(vision)  # type: ignore[arg-type]

    app = build_application(settings.telegram_bot_token)
    handlers = Handlers(
        app,
        orchestrator,
        hitl,
        users,
        pairing,
        repo=repo,
        admin_id=settings.telegram_admin_id,
        router=router_instance,
    )

    orchestrator.on_approval_request = handlers.on_approval_request
    orchestrator.on_status_update = handlers.on_status_update

    register_handlers(app, handlers)

    _wire_webhook(app, handlers, settings, adb)

    # bootstrap_retries: on a throttled link the first getMe can still time
    # out even with the bumped timeouts; retry a handful of times (with PTB's
    # backoff) instead of aborting the whole process on the first miss.
    app.run_polling(bootstrap_retries=5)


def _wire_webhook(app, handlers, settings, adb) -> None:
    """Start an aiohttp trigger server alongside polling, if configured.

    Runs inside Telegram's own event loop via post_init / post_shutdown so we
    don't spawn a second loop. No-op unless WEBHOOK_SECRET is set.
    """
    if not settings.webhook_secret:
        return
    if settings.webhook_owner_user_id is None:
        log.warning(
            "WEBHOOK_SECRET is set but no owner resolved "
            "(set WEBHOOK_OWNER_USER_ID or TELEGRAM_ADMIN_ID). Webhook disabled."
        )
        return

    from aiohttp import web

    from bot.webhook import build_webhook_app

    async def _start(application) -> None:
        # Tunnel the device's localhost:<port> to ours over USB so an
        # on-device trigger reaches the webhook with nothing on the network.
        if await adb.reverse_tcp(settings.webhook_port):
            log.info(
                "adb reverse tcp:%d active — device localhost:%d -> host",
                settings.webhook_port,
                settings.webhook_port,
            )
        web_app = build_webhook_app(
            handlers,
            secret=settings.webhook_secret,
            owner_user_id=settings.webhook_owner_user_id,
        )
        runner = web.AppRunner(web_app)
        await runner.setup()
        site = web.TCPSite(runner, settings.webhook_host, settings.webhook_port)
        await site.start()
        application.bot_data["_webhook_runner"] = runner
        log.info(
            "Trigger webhook listening on http://%s:%d/trigger (owner=%s)",
            settings.webhook_host,
            settings.webhook_port,
            settings.webhook_owner_user_id,
        )

    async def _stop(application) -> None:
        runner = application.bot_data.get("_webhook_runner")
        if runner is not None:
            await runner.cleanup()

    app.post_init = _start
    app.post_shutdown = _stop


if __name__ == "__main__":
    main()
