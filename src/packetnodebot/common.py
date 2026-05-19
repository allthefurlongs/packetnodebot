# Represents a bot connector command that originated not from the remote user, but from inside packetnodebot internals
class InternalBotCommand:
    def __init__(self, command, args=None):
        self.command = command
        self.args = args


# Shared mutable state between BpqInterface and the bot connector. Both sides hold a reference to the same instance,
# so a mutation on one side is observed live by the other (no message round-trip needed).
class BotState:
    def __init__(self, fixed_width=False):
        self.fixed_width = fixed_width


def bytes_str(b):
    return b.decode('utf-8', errors='backslashreplace').replace('\r', '\n')
