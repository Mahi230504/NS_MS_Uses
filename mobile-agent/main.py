"""Entry point: wires config, bot, and agent orchestrator."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from agent.orchestrator import Orchestrator
from agent.persistence import TaskRepository
from agent.providers import make_provider
from agent.skills import SkillRegistry
from bot.handlers import Handlers
from bot.pairing import PairCodeIssuer
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
    )

    app = build_application(settings.telegram_bot_token)
    handlers = Handlers(
        app,
        orchestrator,
        hitl,
        users,
        pairing,
        repo=repo,
        admin_id=settings.telegram_admin_id,
    )

    orchestrator.on_approval_request = handlers.on_approval_request
    orchestrator.on_status_update = handlers.on_status_update

    register_handlers(app, handlers)

    app.run_polling()


if __name__ == "__main__":
    main()
