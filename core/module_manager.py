"""Loads the built-in commands (core/session.py, core/status.py) and every plugin in modules/."""
import importlib
import inspect
import pkgutil

import modules as modules_pkg
from modules.base import BaseModule, Command
from utils.logger import get_logger

log = get_logger("modules")

SYSTEM_MODULES = ("core.session", "core.status")  # built in; everything else is a plugin in modules/


class ModuleManager:
    def __init__(self, ctx):
        self.ctx = ctx
        self.modules: list[BaseModule] = []
        self._commands: dict[str, tuple[Command, BaseModule]] = {}
        self._callbacks: dict[str, tuple[object, BaseModule]] = {}

    def load_modules(self) -> None:
        names = list(SYSTEM_MODULES)
        names += [
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
