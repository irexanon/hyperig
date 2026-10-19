from modules.base import BaseModule, Command


class HelpModule(BaseModule):
    name = "help"
    description = "Dynamic help system"

    def get_commands(self):
        return {"help": Command(self.help, "List commands, or details for one", "/help [command]")}

    async def help(self, args):
        manager = self.ctx.modules
        commands = manager.all_commands()

        if args:
            name = args[0].lstrip("/").lower()
            found = commands.get(name)
            if not found:
                return f"❌ '{name}' not found"
            cmd, module = found
            return f"/{name} ({module.name})\n{cmd.description}\nUsage: {cmd.usage or '/' + name}"

        groups: dict[str, list[str]] = {}
        for name, (cmd, module) in commands.items():
            groups.setdefault(module.name, []).append(f"/{name} - {cmd.description}")
        lines = ["🚀 Hyper Insta commands", ""]
        off = {m.name for m in manager.modules if not manager.is_enabled(m)}
        for module_name, items in groups.items():
            lines.append(module_name.upper() + (" (OFF in Settings)" if module_name in off else ""))
            lines.extend(items)
            lines.append("")
        lines.append("Reply inside a chat topic to answer that Instagram conversation.")
        return "\n".join(lines)
