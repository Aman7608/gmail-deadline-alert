"""Loads all settings from the .env file (or real environment variables)."""

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

# Project root folder (the one that holds .env, credentials.json, etc.)
BASE_DIR = Path(__file__).resolve().parent.parent

DEFAULT_CONTENT_VARIABLES = {"1": "summary", "2": "deadline", "3": "action"}


class ConfigError(Exception):
    """Raised when a required setting is missing or has a bad value."""


def _get_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a whole number, got {raw!r}")


def _get_str(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _resolve_path(raw: str) -> Path:
    path = Path(raw)
    if not path.is_absolute():
        path = BASE_DIR / path
    return path


def _get_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"{name} must be true or false, got {raw!r}")


AI_PROVIDERS = ("claude", "gemini")


def _with_whatsapp_prefix(number: str) -> str:
    if number and not number.startswith("whatsapp:"):
        return "whatsapp:" + number
    return number


@dataclass
class Settings:
    # Gmail
    watch_senders: list[str]
    watch_query: str
    gmail_credentials_file: Path
    gmail_token_file: Path
    lookback_hours: int

    # Claude
    anthropic_api_key: str
    claude_model: str

    # Twilio
    twilio_account_sid: str
    twilio_auth_token: str
    twilio_whatsapp_from: str
    my_whatsapp_to: str
    twilio_voice_from: str
    my_phone_number: str
    twilio_content_sid: str
    twilio_content_variables: dict[str, str]
    twilio_voice: str
    twilio_voice_language: str
    twilio_call_url: str

    # Timing and app
    poll_seconds: int
    no_deadline_call_minutes: int
    timezone_name: str
    db_path: Path
    log_file: Path
    log_level: str
    alert_title: str = "Email alert"

    # Which AI writes the summary: "claude" (paid) or "gemini" (has a free tier)
    ai_provider: str = "claude"
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"

    # Set WHATSAPP_ENABLED=false to only get calls (for example on a Twilio Limited trial)
    whatsapp_enabled: bool = True

    tz: ZoneInfo = field(init=False)

    def __post_init__(self):
        try:
            self.tz = ZoneInfo(self.timezone_name)
        except Exception:
            raise ConfigError(f"TIMEZONE {self.timezone_name!r} is not a valid timezone name")
        if self.ai_provider not in AI_PROVIDERS:
            raise ConfigError(f"AI_PROVIDER must be one of {', '.join(AI_PROVIDERS)}, got {self.ai_provider!r}")

    def check_ready(self, need_gmail=True, need_ai=True, need_twilio=True):
        """Make sure the settings needed for this run mode are filled in."""
        missing = []
        if need_gmail and not self.watch_senders and not self.watch_query:
            missing.append("WATCH_SENDER or WATCH_QUERY")
        if need_ai and self.ai_provider == "claude" and not self.anthropic_api_key:
            missing.append("ANTHROPIC_API_KEY")
        if need_ai and self.ai_provider == "gemini" and not self.gemini_api_key:
            missing.append("GEMINI_API_KEY")
        if need_twilio:
            needed = [
                ("TWILIO_ACCOUNT_SID", self.twilio_account_sid),
                ("TWILIO_AUTH_TOKEN", self.twilio_auth_token),
                ("TWILIO_VOICE_FROM", self.twilio_voice_from),
                ("MY_PHONE_NUMBER", self.my_phone_number),
            ]
            if self.whatsapp_enabled:
                needed += [
                    ("TWILIO_WHATSAPP_FROM", self.twilio_whatsapp_from),
                    ("MY_WHATSAPP_TO", self.my_whatsapp_to),
                ]
            for name, value in needed:
                if not value:
                    missing.append(name)
        if missing:
            raise ConfigError("Missing settings in .env: " + ", ".join(missing))
        if self.poll_seconds < 10:
            raise ConfigError("POLL_SECONDS should be at least 10")


def load_settings(env_file: Path | None = None) -> Settings:
    """Read .env and build a Settings object."""
    load_dotenv(env_file or BASE_DIR / ".env")

    senders = [s.strip().lower() for s in _get_str("WATCH_SENDER").split(",") if s.strip()]

    raw_vars = _get_str("TWILIO_CONTENT_VARIABLES")
    if raw_vars:
        try:
            content_vars = json.loads(raw_vars)
        except json.JSONDecodeError:
            raise ConfigError("TWILIO_CONTENT_VARIABLES must be valid JSON, like {\"1\": \"summary\"}")
        if not isinstance(content_vars, dict):
            raise ConfigError("TWILIO_CONTENT_VARIABLES must be a JSON object")
        content_vars = {str(k): str(v) for k, v in content_vars.items()}
    else:
        content_vars = dict(DEFAULT_CONTENT_VARIABLES)

    return Settings(
        watch_senders=senders,
        watch_query=_get_str("WATCH_QUERY"),
        gmail_credentials_file=_resolve_path(_get_str("GMAIL_CREDENTIALS_FILE", "credentials.json")),
        gmail_token_file=_resolve_path(_get_str("GMAIL_TOKEN_FILE", "token.json")),
        lookback_hours=_get_int("LOOKBACK_HOURS", 24),
        anthropic_api_key=_get_str("ANTHROPIC_API_KEY"),
        claude_model=_get_str("CLAUDE_MODEL", "claude-haiku-5-5") or "claude-haiku-5-5",
        twilio_account_sid=_get_str("TWILIO_ACCOUNT_SID"),
        twilio_auth_token=_get_str("TWILIO_AUTH_TOKEN"),
        twilio_whatsapp_from=_with_whatsapp_prefix(_get_str("TWILIO_WHATSAPP_FROM", "whatsapp:+14155238886")),
        my_whatsapp_to=_with_whatsapp_prefix(_get_str("MY_WHATSAPP_TO")),
        twilio_voice_from=_get_str("TWILIO_VOICE_FROM"),
        my_phone_number=_get_str("MY_PHONE_NUMBER"),
        twilio_content_sid=_get_str("TWILIO_CONTENT_SID"),
        twilio_content_variables=content_vars,
        twilio_voice=_get_str("TWILIO_VOICE", "Polly.Aditi") or "Polly.Aditi",
        twilio_voice_language=_get_str("TWILIO_VOICE_LANGUAGE", "en-IN") or "en-IN",
        twilio_call_url=_get_str("TWILIO_CALL_URL", ""),
        poll_seconds=_get_int("POLL_SECONDS", 60),
        no_deadline_call_minutes=_get_int("NO_DEADLINE_CALL_MINUTES", 60),
        timezone_name=_get_str("TIMEZONE", "Asia/Kolkata") or "Asia/Kolkata",
        db_path=_resolve_path(_get_str("DB_PATH", "data/alerts.db")),
        log_file=_resolve_path(_get_str("LOG_FILE", "logs/app.log")),
        log_level=_get_str("LOG_LEVEL", "INFO").upper() or "INFO",
        alert_title=_get_str("ALERT_TITLE", "Email alert") or "Email alert",
        ai_provider=(_get_str("AI_PROVIDER", "claude") or "claude").lower(),
        gemini_api_key=_get_str("GEMINI_API_KEY"),
        gemini_model=_get_str("GEMINI_MODEL", "gemini-2.5-flash") or "gemini-2.5-flash",
        whatsapp_enabled=_get_bool("WHATSAPP_ENABLED", True),
    )
