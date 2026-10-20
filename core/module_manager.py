"""Loads every plugin in modules/."""
import importlib
import inspect
import pkgutil

import modules as modules_pkg
from modules.base import BaseModule, Command
from utils.logger import get_logger

log = get_logger("modules")


class ModuleManager:
    def __init__(self, ctx):
        self.ctx = ctx
        self.modules: list[BaseModule] = []
        self._commands: dict[str, tuple[Command, BaseModule]] = {}
        self._callbacks: dict[str, tuple[object, BaseModule]] = {}

    def load_modules(self) -> None:
        names = [
            f"modules.{i.name}"
            for i in sorted(pkgutil.iter_modules(modules_pkg.__path__), key=lambda i: i.name)
            if not i.name.startswith("_") and i.name != "base"
        ]
        for name in names:
            try:
                mod = importlib.import_module(name)
                for _, cls in inspect.getmembers(mod, inspect.isclass):
                    if issubclass(cls, BaseModule) and cls is not BaseModule and cls.__module__ == mod.__name__:
                        self.modules.append(cls(self.ctx))
            except Exception:
                log.exception("Failed to load module %s", name)

        for module in self.modules:
            for name, command in module.get_commands().items():
                self._commands[name.lower()] = (command, module)
            for prefix, handler in module.get_callbacks().items():
                self._callbacks[prefix] = (handler, module)
        log.info("Loaded %d modules, %d commands", len(self.modules), len(self._commands))

    def all_commands(self) -> dict[str, tuple[Command, BaseModule]]:
        return self._commands

    def all_callbacks(self) -> dict[str, tuple[object, BaseModule]]:
        return self._callbacks

    def get(self, name: str):
        return next((m for m in self.modules if m.name == name), None)

    def is_enabled(self, module: BaseModule) -> bool:
        """Modules marked toggleable can be switched off in Settings; everything else is always on."""
        if not getattr(module, "toggleable", False):
            return True
        prefs = getattr(self.ctx, "prefs", None)
        return prefs.feature_on(module.name) if prefs else True

    async def handle_text(self, text: str, update) -> bool:
        """Offer a plain private-chat message to every enabled module; the first one that uses it wins."""
        for module in self.modules:
            if not self.is_enabled(module):
                continue
            try:
                if await module.on_text(text, update):
                    return True
            except Exception:
                log.exception("Module %s failed on a text message", module.name)
        return False

    async def start_all(self) -> None:
        for module in self.modules:
            try:
                await module.on_start()
            except Exception:
                log.exception("Module %s failed to start", module.name)

    async def stop_all(self) -> None:
        for module in self.modules:
            try:
                await module.on_stop()
            except Exception:
                log.exception("Module %s failed to stop", module.name)

    async def process_message(self, message):
        """Run a message through every module. A module may return None to drop it."""
        for module in self.modules:
            try:
                message = await module.process(message)
            except Exception:
                log.exception("Module %s failed on a message", module.name)
                continue
            if message is None:
                return None
        return message
