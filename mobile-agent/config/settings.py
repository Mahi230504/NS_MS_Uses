"""Loads from .env, single config object used everywhere."""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


_REQUIRED = (
    "GEMINI_API_KEY",
    "TELEGRAM_BOT_TOKEN",
    "ANDROID_DEVICE_ID",
)

CONFIG_DIR_ENV = "MOBILE_AGENT_CONFIG_DIR"
DEFAULT_CONFIG_DIR = Path.home() / ".mobile-agent"


@dataclass(frozen=True)
class Settings:
    gemini_api_key: str
    gemini_model: str
    vision_provider: str
    telegram_bot_token: str
    telegram_admin_id: int | None
    telegram_allowed_user_ids_env: frozenset[int]
    android_device_id: str
    session_timeout_seconds: int
    log_dir: Path
    config_dir: Path
    users_path: Path
    db_path: Path
    allow_physical_device: bool


def _resolve_config_dir() -> Path:
    """Return the directory that holds .env, users.json, logs/.

    Override via MOBILE_AGENT_CONFIG_DIR; defaults to ~/.mobile-agent. Created
    on first call, with .env.example seeded if not already present.
    """
    override = os.environ.get(CONFIG_DIR_ENV)
    config_dir = Path(override).expanduser() if override else DEFAULT_CONFIG_DIR
    config_dir.mkdir(parents=True, exist_ok=True)

    # Seed .env.example so a fresh user can `cp ~/.mobile-agent/.env.example
    # ~/.mobile-agent/.env` and start editing.
    seed_src = Path(__file__).resolve().parent.parent / ".env.example"
    seed_dst = config_dir / ".env.example"
    if seed_src.exists() and not seed_dst.exists():
        try:
            shutil.copy(seed_src, seed_dst)
        except OSError:
            pass  # non-fatal — user can copy manually
    return config_dir


def _pick_dotenv(config_dir: Path, explicit: os.PathLike | str | None) -> Path | None:
    if explicit is not None:
        return Path(explicit)
    candidate = config_dir / ".env"
    if candidate.exists():
        return candidate
    cwd = Path.cwd() / ".env"
    if cwd.exists():
        return cwd
    return None


def load_settings(dotenv_path: str | os.PathLike | None = None) -> Settings:
    """Load and validate settings. Raises RuntimeError listing all missing keys."""
    config_dir = _resolve_config_dir()
    chosen = _pick_dotenv(config_dir, dotenv_path)
    if chosen is not None:
        load_dotenv(chosen)
    else:
        # Still call load_dotenv() for the side-effect of reading any cwd .env
        # that might appear after we checked (rare, but cheap).
        load_dotenv()

    missing = [k for k in _REQUIRED if not os.environ.get(k)]
    if missing:
        raise RuntimeError(
            "Missing required env vars: " + ", ".join(missing) + ". "
            f"Edit {config_dir / '.env'} (template at .env.example)."
        )

    raw_admin = os.environ.get("TELEGRAM_ADMIN_ID", "").strip()
    admin_id: int | None = None
    if raw_admin:
        try:
            admin_id = int(raw_admin)
        except ValueError as e:
            raise RuntimeError(f"TELEGRAM_ADMIN_ID must be an integer: {e}")

    raw_ids = os.environ.get("TELEGRAM_ALLOWED_USER_IDS", "").strip()
    try:
        user_ids = frozenset(
            int(x.strip()) for x in raw_ids.split(",") if x.strip()
        )
    except ValueError as e:
        raise RuntimeError(
            f"TELEGRAM_ALLOWED_USER_IDS must be a comma-separated list of integers: {e}"
        )

    try:
        timeout = int(os.environ.get("SESSION_TIMEOUT_SECONDS", "180"))
    except ValueError as e:
        raise RuntimeError(f"SESSION_TIMEOUT_SECONDS must be an integer: {e}")

    raw_log_dir = os.environ.get("LOG_DIR", "").strip()
    log_dir = Path(raw_log_dir) if raw_log_dir else (config_dir / "logs")

    # Opt-in override of the emulator-only safety rule. Set to "1" / "true" /
    # "yes" to run on a physical device. Leave empty (default) to refuse.
    allow_physical = os.environ.get("ALLOW_PHYSICAL_DEVICE", "").strip().lower() in (
        "1", "true", "yes", "on"
    )

    return Settings(
        gemini_api_key=os.environ["GEMINI_API_KEY"],
        gemini_model=os.environ.get("GEMINI_MODEL", "gemini-2.0-flash"),
        vision_provider=os.environ.get("VISION_PROVIDER", "gemini"),
        telegram_bot_token=os.environ["TELEGRAM_BOT_TOKEN"],
        telegram_admin_id=admin_id,
        telegram_allowed_user_ids_env=user_ids,
        android_device_id=os.environ["ANDROID_DEVICE_ID"],
        session_timeout_seconds=timeout,
        log_dir=log_dir,
        config_dir=config_dir,
        users_path=config_dir / "users.json",
        db_path=config_dir / "tasks.db",
        allow_physical_device=allow_physical,
    )
