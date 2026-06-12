"""Loads from .env, single config object used everywhere."""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


# Base requirements (every provider). Per-provider requirements are checked
# separately so e.g. Vertex (service-account auth) doesn't need GEMINI_API_KEY.
_REQUIRED = (
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
    # Vertex-only (empty otherwise).
    gcp_project_id: str
    gcp_location: str
    # OpenRouter-only (empty otherwise).
    openrouter_api_key: str
    openrouter_model: str
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
    enable_vision_hitl: bool
    # External trigger webhook (iOS Siri Shortcut, etc.). Disabled when the
    # secret is empty.
    webhook_secret: str
    webhook_owner_user_id: int | None
    webhook_host: str
    webhook_port: int
    # Cross-app comparison (#6): how many candidate apps a single comparison
    # probes. Each probe is a full agent run on the one device, so this bounds
    # latency. Only used when the provider supports text completion (router/
    # classifier path).
    comparison_max_candidates: int
    # IANA timezone the user's schedules (#5) are expressed in. Local wall-clock
    # ("every day at 9am") is interpreted in this tz and stored as absolute UTC.
    timezone: str
    # Personal-assistant dashboard (#6 visualization). Disabled when the token is
    # empty (no port opened). Bound to localhost by default like the webhook.
    dashboard_token: str
    dashboard_host: str
    dashboard_port: int
    dashboard_cors_origin: str
    dashboard_dist_dir: str
    # Google cloud actions (Gmail send + Meet scheduling). Disabled when the
    # OAuth client id/secret are empty — handlers and dashboard degrade to a
    # "connect Google from Settings" hint instead.
    google_client_id: str
    google_client_secret: str
    google_redirect_uri: str
    google_token_path: Path


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

    provider = os.environ.get("VISION_PROVIDER", "gemini").strip() or "gemini"

    # Per-provider required vars. Validated alongside the base list so the
    # user gets one error listing everything that's missing.
    provider_required: list[str] = []
    if provider == "gemini":
        provider_required = ["GEMINI_API_KEY"]
    elif provider == "vertex":
        provider_required = ["GCP_PROJECT_ID", "GOOGLE_APPLICATION_CREDENTIALS"]
    elif provider == "openrouter":
        provider_required = ["OPENROUTER_API_KEY"]
    else:
        raise RuntimeError(
            f"Unsupported VISION_PROVIDER: {provider!r}. "
            "Supported: 'gemini', 'vertex', 'openrouter'."
        )

    missing = [k for k in (*_REQUIRED, *provider_required) if not os.environ.get(k)]
    if missing:
        raise RuntimeError(
            "Missing required env vars: " + ", ".join(missing) + ". "
            f"Edit {config_dir / '.env'} (template at .env.example)."
        )

    # For Vertex specifically: validate the credentials file actually exists.
    # ADC will fail with a less-helpful error otherwise.
    if provider == "vertex":
        cred_path = Path(os.environ["GOOGLE_APPLICATION_CREDENTIALS"]).expanduser()
        if not cred_path.is_file():
            raise RuntimeError(
                f"GOOGLE_APPLICATION_CREDENTIALS points to {cred_path}, "
                "which doesn't exist or isn't a file."
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

    # External-trigger webhook. The secret gates the endpoint; an empty secret
    # disables the server entirely (no port is opened). A trigger maps to a
    # single owner user — explicit WEBHOOK_OWNER_USER_ID, else the admin.
    webhook_secret = os.environ.get("WEBHOOK_SECRET", "").strip()
    raw_owner = os.environ.get("WEBHOOK_OWNER_USER_ID", "").strip()
    if raw_owner:
        try:
            webhook_owner_user_id: int | None = int(raw_owner)
        except ValueError as e:
            raise RuntimeError(f"WEBHOOK_OWNER_USER_ID must be an integer: {e}")
    else:
        webhook_owner_user_id = admin_id
    webhook_host = os.environ.get("WEBHOOK_HOST", "127.0.0.1").strip() or "127.0.0.1"
    try:
        webhook_port = int(os.environ.get("WEBHOOK_PORT", "8765"))
    except ValueError as e:
        raise RuntimeError(f"WEBHOOK_PORT must be an integer: {e}")

    try:
        comparison_max_candidates = int(
            os.environ.get("COMPARISON_MAX_CANDIDATES", "3")
        )
    except ValueError as e:
        raise RuntimeError(f"COMPARISON_MAX_CANDIDATES must be an integer: {e}")
    comparison_max_candidates = max(2, comparison_max_candidates)

    tz_name = os.environ.get("TIMEZONE", "Asia/Kolkata").strip() or "Asia/Kolkata"
    try:
        from zoneinfo import ZoneInfo

        ZoneInfo(tz_name)  # validate early — bad tz fails fast, not at fire-time
    except Exception as e:
        raise RuntimeError(f"TIMEZONE {tz_name!r} is not a valid IANA zone: {e}")

    dashboard_token = os.environ.get("DASHBOARD_TOKEN", "").strip()
    dashboard_host = os.environ.get("DASHBOARD_HOST", "127.0.0.1").strip() or "127.0.0.1"
    try:
        dashboard_port = int(os.environ.get("DASHBOARD_PORT", "8770"))
    except ValueError as e:
        raise RuntimeError(f"DASHBOARD_PORT must be an integer: {e}")
    dashboard_cors_origin = (
        os.environ.get("DASHBOARD_CORS_ORIGIN", "http://localhost:5173").strip()
        or "http://localhost:5173"
    )
    dashboard_dist_dir = os.environ.get(
        "DASHBOARD_DIST_DIR",
        str(Path(__file__).resolve().parent.parent / "dashboard" / "dist"),
    ).strip()

    # Google OAuth client for cloud actions. Empty id/secret disables the
    # feature (no auth manager is constructed). The redirect default points at
    # the dashboard's own callback route, so it must interpolate the parsed
    # dashboard_port — keep this after the DASHBOARD_PORT block above.
    google_client_id = os.environ.get("GOOGLE_CLIENT_ID", "").strip()
    google_client_secret = os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()
    google_redirect_uri = (
        os.environ.get("GOOGLE_REDIRECT_URI", "").strip()
        or f"http://127.0.0.1:{dashboard_port}/api/google/oauth/callback"
    )

    # Opt-in override of the emulator-only safety rule. Set to "1" / "true" /
    # "yes" to run on a physical device. Leave empty (default) to refuse.
    allow_physical = os.environ.get("ALLOW_PHYSICAL_DEVICE", "").strip().lower() in (
        "1", "true", "yes", "on"
    )

    # Vision-augmented HITL doubles Gemini calls per state-changing action
    # (one for the action, one to classify whether the screen is sensitive).
    # On strict free tiers (20 RPD on gemini-2.5-flash) that halves usable
    # tasks/day. Default OFF; keyword filter still gates payment/OTP/etc.
    enable_vision_hitl = os.environ.get("ENABLE_VISION_HITL", "").strip().lower() in (
        "1", "true", "yes", "on"
    )

    return Settings(
        gemini_api_key=os.environ.get("GEMINI_API_KEY", ""),
        gemini_model=os.environ.get("GEMINI_MODEL", "gemini-2.0-flash"),
        vision_provider=provider,
        gcp_project_id=os.environ.get("GCP_PROJECT_ID", ""),
        gcp_location=os.environ.get("GCP_LOCATION", "us-central1"),
        openrouter_api_key=os.environ.get("OPENROUTER_API_KEY", ""),
        openrouter_model=os.environ.get("OPENROUTER_MODEL", "google/gemini-2.5-flash"),
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
        enable_vision_hitl=enable_vision_hitl,
        webhook_secret=webhook_secret,
        webhook_owner_user_id=webhook_owner_user_id,
        webhook_host=webhook_host,
        webhook_port=webhook_port,
        comparison_max_candidates=comparison_max_candidates,
        timezone=tz_name,
        dashboard_token=dashboard_token,
        dashboard_host=dashboard_host,
        dashboard_port=dashboard_port,
        dashboard_cors_origin=dashboard_cors_origin,
        dashboard_dist_dir=dashboard_dist_dir,
        google_client_id=google_client_id,
        google_client_secret=google_client_secret,
        google_redirect_uri=google_redirect_uri,
        google_token_path=config_dir / "google_token.json",
    )
