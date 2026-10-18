"""Per-account preferences (feature switches, pacing, notifications, downloads).

Saved in the Instagram account's own store under "prefs", so every account keeps its own settings and they
survive restarts. The Settings panel in Telegram edits them; modules read them through ctx.prefs.
"""
from copy import deepcopy
from typing import Any

from utils.logger import get_logger

log = get_logger("prefs")

PACES = ("safe", "normal", "fast")

DEFAULTS: dict[str, Any] = {
    "features": {},          # module name -> bool; a missing name means ON
    "pace": "normal",        # safe | normal | fast: how gently the watcher talks to Instagram
    "alerts_to": "dm",       # dm | group: where watcher alerts go
    "digest": "instant",     # instant | daily: send alerts as they happen, or one summary a day
    "digest_hour": 20,       # local hour of the daily summary
    "quiet": False,          # hold alerts back during quiet hours (sent afterwards as one message)
    "quiet_start": 23,       # local hour quiet hours begin
    "quiet_end": 7,          # local hour quiet hours end
    "tz_hours": None,        # hours from UTC for the three settings above; None = the account's timezone setting
    "dl_audio": True,        # offer "download original audio" under downloaded Reels
}


class Prefs:
    def __init__(self, store):
        self.store = store
        self.data: dict[str, Any] = deepcopy(DEFAULTS)

    async def load(self) -> None:
        try:
            saved = await self.store.get("prefs", {}) or {}
        except Exception:
            log.exception("Could not read preferences, using defaults")
            return
        for key, value in saved.items():
            if key in DEFAULTS:
                self.data[key] = value
        if not isinstance(self.data.get("features"), dict):
            self.data["features"] = {}

    async def _save(self) -> None:
        await self.store.set("prefs", self.data)

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, DEFAULTS.get(key, default))

    async def set(self, key: str, value: Any) -> None:
        if key not in DEFAULTS:
            raise KeyError(key)
        self.data[key] = value
        await self._save()

    # ---- feature switches
    def feature_on(self, name: str) -> bool:
        return bool(self.data["features"].get(name, True))

    async def set_feature(self, name: str, on: bool) -> None:
        self.data["features"][name] = bool(on)
        await self._save()

    # ---- time helpers (quiet hours / digest are in the owner's local time)
    def tz_hours(self, cfg=None) -> float:
        value = self.data.get("tz_hours")
        if value is not None:
            return float(value)
        seconds = getattr(cfg, "ig_timezone_offset", None)
        return float(seconds) / 3600 if seconds else 0.0
