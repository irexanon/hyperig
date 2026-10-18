"""Configuration. Everything comes from environment variables (.env file supported).

No secrets live in source code. Copy .env.example to .env and fill it in.
"""
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _get(name: str, default: str | None = None, required: bool = False) -> str | None:
    value = os.getenv(name, default)
    if required and not value:
        raise RuntimeError(f"Missing required setting {name}. See .env.example")
    return value or None if not required else value


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _int_or_none(name: str) -> int | None:
    raw = os.getenv(name)
    return int(raw) if raw and raw.strip() else None


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


def _int_or_default(name: str, default: int) -> int:
    raw = os.getenv(name)
    if not raw or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default


@dataclass(frozen=True)
class Config:
    # ---- Instagram: credentials
    ig_username: str
    ig_password: str | None
    ig_totp_secret: str | None      # base32 secret of your authenticator app (2FA)
    ig_sessionid: str | None        # optional: bootstrap the very first login from a sessionid
    ig_phone_number: str | None     # for "confirm your phone" (submit_phone) challenges

    # ---- Instagram: network + device identity (applied only when a NEW device is created)
    ig_proxy: str | None
    ig_locale: str | None           # e.g. en_US
    ig_country: str | None          # e.g. US
    ig_country_code: int | None     # calling code, e.g. 1
    ig_timezone_offset: int | None  # seconds from UTC, e.g. 18000 for Pakistan
    ig_app_version: str | None      # app version to present as (built-in profile, or custom with the two below)
    ig_app_version_code: str | None  # Android versionCode of that app version (custom profiles)
    ig_bloks_versioning_id: str | None  # Bloks hash of that app version (custom profiles; needed for the CAA login)
    ig_override_app_version: bool   # upgrade an old saved session to the supported app profile
    ig_legacy_login: bool           # use login_legacy() instead of the default CAA flow
    ig_cookie_upgrade: bool         # after /cookie login, try a password login on top (falls back safely)
    ig_delay_range: list[float]     # random pause after each request, seconds
    ig_tls_verify: bool | str

    # ---- Instagram: behaviour
    poll_interval: int              # seconds between inbox checks
    include_requests: bool          # also bridge the "message requests" folder
    realtime: bool                  # live delivery over instagrapi's MQTT realtime client
    fbns: bool                      # push-notification channel (FBNS), wakes the inbox sync
    realtime_send: bool             # send Telegram text replies over MQTT instead of HTTP
    safety_poll_interval: int       # seconds between inbox checks while realtime is connected
    mark_seen: bool                 # tell the sender you read their message once it reaches Telegram
    max_media_mb: int               # largest file moved either way
    code_timeout: int               # seconds to wait for you to send /code

    # ---- Telegram
    tg_token: str
    tg_chat_id: int                 # the forum-enabled group (starts with -100)
    tg_admin_id: int                # your numeric Telegram user id

    # ---- Storage
    mongo_uri: str | None           # if empty, JSON files in data_dir are used
    mongo_db: str
    data_dir: Path

    log_level: str

    @property
    def missing_settings(self) -> list[str]:
        missing: list[str] = []
        if not self.ig_username:
            missing.append("IG_USERNAME")
        if not self.ig_password and not self.ig_sessionid:
            missing.append("IG_PASSWORD (or Cookie)")
        if not self.tg_token:
            missing.append("TELEGRAM_BOT_TOKEN")
        if not self.tg_chat_id:
            missing.append("TELEGRAM_CHAT_ID")
        return missing

    @property
    def ig_configured(self) -> bool:
        return bool(self.ig_username and (self.ig_password or self.ig_sessionid))

    @property
    def tg_configured(self) -> bool:
        return bool(self.tg_token and self.tg_chat_id)

    def to_public_dict(self) -> dict:
        return {
            "ig_username": self.ig_username,
            "has_password": bool(self.ig_password),
            "has_totp_secret": bool(self.ig_totp_secret),
            "has_sessionid": bool(self.ig_sessionid),
            "ig_phone_number": self.ig_phone_number or "",
            "ig_proxy": self.ig_proxy or "",
            "ig_locale": self.ig_locale or "en_US",
            "ig_country": self.ig_country or "US",
            "ig_country_code": self.ig_country_code if self.ig_country_code is not None else "",
            "ig_timezone_offset": self.ig_timezone_offset if self.ig_timezone_offset is not None else "",
            "ig_app_version": self.ig_app_version or "",
            "ig_app_version_code": self.ig_app_version_code or "",
            "ig_bloks_versioning_id": self.ig_bloks_versioning_id or "",
            "ig_override_app_version": self.ig_override_app_version,
            "ig_legacy_login": self.ig_legacy_login,
            "ig_cookie_upgrade": self.ig_cookie_upgrade,
            "ig_delay_range": f"{self.ig_delay_range[0]},{self.ig_delay_range[1]}",
            "poll_interval": self.poll_interval,
            "safety_poll_interval": self.safety_poll_interval,
            "include_requests": self.include_requests,
            "realtime": self.realtime,
            "fbns": self.fbns,
            "realtime_send": self.realtime_send,
            "mark_seen": self.mark_seen,
            "max_media_mb": self.max_media_mb,
            "code_timeout": self.code_timeout,
            "tg_chat_id": self.tg_chat_id or "",
            "tg_admin_id": self.tg_admin_id or "",
            "has_tg_token": bool(self.tg_token),
        }


def build_user_config(base: Config, saved: dict, username: str) -> Config:
    """Merge persisted per-user settings on top of base environment defaults."""
    def _opt_str(key: str, fallback: str | None) -> str | None:
        if key not in saved:
            return fallback
        val = str(saved.get(key) or "").strip()
        return val if val else None

    def _opt_int(key: str, fallback: int | None) -> int | None:
        if key not in saved:
            return fallback
        raw = str(saved.get(key) or "").strip()
        if not raw:
            return None
        try:
            return int(raw)
        except ValueError:
            return fallback

    def _opt_bool(key: str, fallback: bool) -> bool:
        if key not in saved or saved.get(key) is None:
            return fallback
        val = saved.get(key)
        if isinstance(val, bool):
            return val
        return str(val).strip().lower() in ("1", "true", "yes", "on")

    delay = base.ig_delay_range
    if saved.get("ig_delay_range"):
        try:
            low, high = (float(x) for x in str(saved["ig_delay_range"]).split(","))
            if 0 <= low <= high:
                delay = [low, high]
        except Exception:
            pass

    poll_int = _opt_int("poll_interval", base.poll_interval)
    safety_int = _opt_int("safety_poll_interval", base.safety_poll_interval)
    max_mb = _opt_int("max_media_mb", base.max_media_mb)
    code_to = _opt_int("code_timeout", base.code_timeout)

    tg_token = _opt_str("tg_token", None) or base.tg_token
    tg_chat_id = _opt_int("tg_chat_id", 0) or 0
    tg_admin_id = _opt_int("tg_admin_id", 0) or 0

    return Config(
        ig_username=( _opt_str("ig_username", None) or username ).lstrip("@").strip(),
        ig_password=_opt_str("ig_password", None),
        ig_totp_secret=_opt_str("ig_totp_secret", None),
        ig_sessionid=_opt_str("ig_sessionid", None),
        ig_phone_number=_opt_str("ig_phone_number", None),
        ig_proxy=_opt_str("ig_proxy", base.ig_proxy),
        ig_locale=_opt_str("ig_locale", base.ig_locale),
        ig_country=_opt_str("ig_country", base.ig_country),
        ig_country_code=_opt_int("ig_country_code", base.ig_country_code),
        ig_timezone_offset=_opt_int("ig_timezone_offset", base.ig_timezone_offset),
        ig_app_version=_opt_str("ig_app_version", base.ig_app_version),
        ig_app_version_code=_opt_str("ig_app_version_code", base.ig_app_version_code),
        ig_bloks_versioning_id=_opt_str("ig_bloks_versioning_id", base.ig_bloks_versioning_id),
        ig_override_app_version=_opt_bool("ig_override_app_version", base.ig_override_app_version),
        ig_legacy_login=_opt_bool("ig_legacy_login", base.ig_legacy_login),
        ig_cookie_upgrade=_opt_bool("ig_cookie_upgrade", base.ig_cookie_upgrade),
        ig_delay_range=delay,
        ig_tls_verify=base.ig_tls_verify,
        poll_interval=max(15, poll_int if poll_int is not None else 30),
        include_requests=_opt_bool("include_requests", base.include_requests),
        realtime=_opt_bool("realtime", base.realtime),
        fbns=_opt_bool("fbns", base.fbns),
        realtime_send=_opt_bool("realtime_send", base.realtime_send),
        safety_poll_interval=max(60, safety_int if safety_int is not None else 300),
        mark_seen=_opt_bool("mark_seen", base.mark_seen),
        max_media_mb=max(1, min(50, max_mb if max_mb is not None else 20)),
        code_timeout=max(60, code_to if code_to is not None else 300),
        tg_token=tg_token,
        tg_chat_id=tg_chat_id,
        tg_admin_id=tg_admin_id,
        mongo_uri=base.mongo_uri,
        mongo_db=base.mongo_db,
        data_dir=base.data_dir / "users" / username,
        log_level=base.log_level,
    )


def load_config(strict: bool = False) -> Config:
    sessionid = _get("IG_SESSIONID")
    password = _get("IG_PASSWORD")
    if strict and not password and not sessionid:
        raise RuntimeError("Set IG_PASSWORD (recommended) or IG_SESSIONID. See .env.example")

    return Config(
        ig_username=_get("IG_USERNAME", required=strict) or "",
        ig_password=password,
        ig_totp_secret=_get("IG_TOTP_SECRET"),
        ig_sessionid=sessionid,
        ig_phone_number=_get("IG_PHONE_NUMBER"),
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
        ig_cookie_upgrade=_bool("IG_COOKIE_UPGRADE", True),
        ig_delay_range=_delay_range("IG_DELAY_RANGE", "1,3"),
        ig_tls_verify=_tls_verify("IG_TLS_VERIFY"),
        poll_interval=max(15, _int_or_default("POLL_INTERVAL", 30)),
        include_requests=_bool("INCLUDE_REQUESTS", True),
        realtime=_bool("IG_REALTIME", True),
        fbns=_bool("IG_FBNS", True),
        realtime_send=_bool("IG_REALTIME_SEND", True),
        safety_poll_interval=max(60, _int_or_default("SAFETY_POLL_INTERVAL", 300)),
        mark_seen=_bool("MARK_SEEN", False),
        max_media_mb=max(1, min(50, _int_or_default("MAX_MEDIA_MB", 20))),
        code_timeout=max(60, _int_or_default("CODE_TIMEOUT", 300)),
        tg_token=_get("TELEGRAM_BOT_TOKEN", required=strict) or "",
        tg_chat_id=int(_get("TELEGRAM_CHAT_ID", required=True)) if strict else _int_or_default("TELEGRAM_CHAT_ID", 0),
        tg_admin_id=int(_get("TELEGRAM_ADMIN_ID", required=True)) if strict else _int_or_default("TELEGRAM_ADMIN_ID", 0),
        mongo_uri=_get("MONGO_URI"),
        mongo_db=_get("MONGO_DB", "hyperinsta") or "hyperinsta",
        data_dir=Path(_get("DATA_DIR", "./data") or "./data"),
        log_level=(_get("LOG_LEVEL", "INFO") or "INFO").upper(),
    )
