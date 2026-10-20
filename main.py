"""Fetch IG entry point."""
from config import load_config
from tg.bot import FetchIGBot
from utils.db import create_store
from utils.logger import get_logger, setup_logging


def main() -> None:
    cfg = load_config()
    setup_logging(cfg.log_level)
    log = get_logger("main")
    bot = FetchIGBot(cfg, create_store(cfg))
    log.info("Starting Fetch IG")
    bot.build().run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
