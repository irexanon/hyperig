import contextvars
import logging
import threading
from collections import deque
from datetime import datetime, timezone

current_user_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("current_user", default=None)

_LOG_LOCK = threading.Lock()
_USER_LOGS: dict[str, deque[dict]] = {}
_LOG_SEQ: dict[str, int] = {}
MAX_USER_LOGS = 400


def add_user_log(username: str, level: str, logger_name: str, message: str) -> None:
    if not username:
        return
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with _LOG_LOCK:
        buf = _USER_LOGS.setdefault(username, deque(maxlen=MAX_USER_LOGS))
        seq = _LOG_SEQ.get(username, 0) + 1
        _LOG_SEQ[username] = seq
        buf.append(
            {
                "id": seq,
                "timestamp": now,
                "level": level.upper(),
                "logger": logger_name,
                "message": message,
            }
        )


def get_user_logs(username: str, since_id: int = 0) -> list[dict]:
    with _LOG_LOCK:
        buf = _USER_LOGS.get(username)
        if not buf:
            return []
        return [item for item in buf if item["id"] > since_id]


def clear_user_logs(username: str) -> None:
    with _LOG_LOCK:
        if username in _USER_LOGS:
            _USER_LOGS[username].clear()


class _UserCaptureHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        user = current_user_var.get()
        if not user:
            return
        try:
            msg = record.getMessage()
            if record.exc_info:
                msg = f"{msg} ({record.exc_info[1]})"
            add_user_log(user, record.levelname, record.name, msg)
        except Exception:
            pass


_capture_installed = False


def setup_logging(level: str = "INFO") -> None:
    global _capture_installed
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    if not _capture_installed:
        logging.getLogger().addHandler(_UserCaptureHandler())
        _capture_installed = True
    # httpx logs full request URLs at INFO, and Telegram URLs contain the bot token.
    for noisy in ("httpx", "httpcore", "telegram.ext.Updater"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    if level != "DEBUG":
        for name in ("instagrapi", "public_request", "private_request"):
            logging.getLogger(name).setLevel(logging.CRITICAL)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
