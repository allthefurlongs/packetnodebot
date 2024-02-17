# Represents a bot connector command that originated not from the remote user, but from inside packetnodebot internals
class InternalBotCommand:
    def __init__(self, command, args=None):
        self.command = command
        self.args = args
