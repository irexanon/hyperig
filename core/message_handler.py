from utils.logger import get_logger

log = get_logger("handler")


class MessageHandler:
    """Instagram message -> modules -> Telegram. Raises if Telegram delivery fails
    so the Instagram poller knows to retry instead of losing the message."""

    def __init__(self, module_manager, bridge):
        self.modules = module_manager
        self.bridge = bridge

    async def handle(self, message) -> None:
        message = await self.modules.process_message(message)
        if message is None:
            log.info("Message dropped by a module")
            return
        await self.bridge.send_to_telegram(message)
