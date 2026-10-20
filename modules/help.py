from modules.base import BaseModule, Command


class HelpModule(BaseModule):
    name = "help"
    description = "Command list"

    def get_commands(self):
        return {"help": Command(self.help, "List commands, or details for one", "/help [command]")}

    async def help(self, args):
        manager = self.ctx.modules
        commands = manager.all_commands()
        if args:
            name = args[0].lstrip("/").lower()
            found = commands.get(name)
            if not found:
                return f"No command named '{name}'."
            cmd, _module = found
            return f"/{name}\n{cmd.description}\nUsage: {cmd.usage or '/' + name}"

        lines = ["Fetch IG commands", ""]
        for name, (cmd, _module) in commands.items():
            lines.append(f"/{name}  {cmd.description}")
        lines += ["", "You can also paste an Instagram link and it will be downloaded."]
        return "\n".join(lines)
