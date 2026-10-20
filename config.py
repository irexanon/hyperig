"""Configuration. Everything comes from environment variables (.env file supported).

Only the bot itself is configured here. Instagram accounts are connected by each user inside the bot
(password or cookie), so no Instagram credentials live in the environment.
"""
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _get(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name, default)
    return value.strip() or None if value else None


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _int_or_none(name: str) -> int | None:
    raw = os.getenv(name)
    try:
        return int(raw) if raw and raw.strip() else None
    except ValueError:
        return None


def _int(name: str, default: int) -> int:
    value = _int_or_none(name)
    return default if value is None else value


def _delay_range(name: str, default: str) -> list[float]:
    raw = os.getenv(name) or default
    try:
        low, high = (float(x) for x in raw.split(","))
    except ValueError:
        raise RuntimeError(f"{name} must look like '1,3' (min,max seconds)")
    if low < 0 or high < low:
        raise RuntimeError(f"{name} must satisfy 0 <= min <= max")
    return [low, high]


def _tls_verify(name: str) -> bool | str:
    raw = (os.getenv(name) or "true").strip()
    if raw.lower() in ("1", "true", "yes", "on"):
        return True
    if raw.lower() in ("0", "false", "no", "off"):
        return False
    return raw  # path to a CA bundle (e.g. for a proxy that re-signs TLS)


@dataclass(frozen=True)
class Config:
    # ---- Telegram
    tg_token: str = ""

    # ---- Instagram identity of the connected user. Filled in per user by the bot, never from the environment.
    ig_username: str = ""
    ig_password: str | None = None      # only held in memory while logging in, never saved
    ig_totp_secret: str | None = None
    ig_sessionid: str | None = None
    ig_phone_number: str | None = None

    # ---- Instagram: network + device identity (applied only when a NEW device is created)
    ig_proxy: str | None = None
    ig_locale: str | None = None        # e.g. en_US
    ig_country: str | None = None       # e.g. US
    ig_country_code: int | None = None  # calling code, e.g. 1
    ig_timezone_offset: int | None = None  # seconds from UTC, e.g. 18000 for Pakistan
    ig_app_version: str | None = None
    ig_app_version_code: str | None = None
    ig_bloks_versioning_id: str | None = None
    ig_override_app_version: bool = False
    ig_legacy_login: bool = False
    ig_cookie_upgrade: bool = False     # never: passwords are not kept, so there is nothing to upgrade with
    ig_delay_range: tuple = (1.0, 3.0)  # random pause after each request, seconds
    ig_tls_verify: bool | str = True

    # ---- Behaviour
    max_media_mb: int = 50              # Telegram bots can upload up to 50 MB
    code_timeout: int = 300             # seconds to wait for a 2FA / verification code
    max_watch: int = 10                 # watched accounts per user

    # ---- Storage
    mongo_uri: str | None = None        # if empty, JSON files in data_dir are used
    mongo_db: str = "fetchig"
    data_dir: Path = Path("./data")

    log_level: str = "INFO"


def load_config() -> Config:
    token = _get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("Missing required setting TELEGRAM_BOT_TOKEN. See .env.example")
    return Config(
        tg_token=token,
        ig_proxy=_get("IG_PROXY"),
        ig_locale=_get("IG_LOCALE"),
        ig_country=_get("IG_COUNTRY"),
        ig_country_code=_int_or_none("IG_COUNTRY_CODE"),
        ig_timezone_offset=_int_or_none("IG_TIMEZONE_OFFSET"),
        ig_app_version=_get("IG_APP_VERSION"),
        ig_app_version_code=_get("IG_APP_VERSION_CODE"),
        ig_bloks_versioning_id=_get("IG_BLOKS_VERSIONING_ID"),
        ig_override_app_version=_bool("IG_OVERRIDE_APP_VERSION", False),
        ig_legacy_login=_bool("IG_LEGACY_LOGIN", False),
        ig_delay_range=tuple(_delay_range("IG_DELAY_RANGE", "1,3")),
        ig_tls_verify=_tls_verify("IG_TLS_VERIFY"),
        max_media_mb=max(1, min(50, _int("MAX_MEDIA_MB", 50))),
        code_timeout=max(60, _int("CODE_TIMEOUT", 300)),
        max_watch=max(1, _int("MAX_WATCH", 10)),
        mongo_uri=_get("MONGO_URI"),
        mongo_db=_get("MONGO_DB", "fetchig") or "fetchig",
        data_dir=Path(_get("DATA_DIR", "./data") or "./data"),
        log_level=(_get("LOG_LEVEL", "INFO") or "INFO").upper(),
    )
