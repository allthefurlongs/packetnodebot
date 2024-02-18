from io import StringIO

# Represents a bot connector command that originated not from the remote user, but from inside packetnodebot internals
class InternalBotCommand:
    def __init__(self, command, args=None):
        self.command = command
        self.args = args


def bytes_str(b):
        string = StringIO()
        for byte in b:
            decoded = bytes([byte]).decode('utf-8', 'ignore')
            if byte == 13:  # Convert \r to \n as it works better for bot messages anyway
                string.write('\n')
            elif len(decoded) == 0:
                string.write('\\x{:02X}'.format(byte))
            else:
                string.write(decoded)
        return string.getvalue()
