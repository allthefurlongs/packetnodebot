import asyncio
import yaml
from io import BytesIO
import discord
import packetnodebot.discord


class BpqInterface():
    def __init__(self, bot_in_queue, bot_out_queue):
        self.bot_in_queue = bot_in_queue
        self.bot_out_queue = bot_out_queue
        self.telnet_passthru_task = None
        self.telnet_in_queue = None  # Used for telnet passthru, set to an asyncio.Queue when in use

        self.fbb_state = {
            'monitoring': False,
            'alerts': {
                'calls_seen': set(),
                'calls_connected': set()
            }
        }
        self.fbb_connection_task = None
        self.fbb_writer = None
        self.fbb_reader = None

    async def add_alert_call_seen(self, callsign):
        if not self.fbb_state['monitoring']:
            await self.fbb_start_monitor()
        callsign = callsign.upper()
        self.fbb_state['alerts']['calls_seen'].add(callsign)
        await self.bot_out_queue.put(f"Alert added for {callsign} seen on air")

    async def add_alert_call_connected(self, callsign):
        if not self.fbb_state['monitoring']:
            await self.fbb_start_monitor()
        callsign = callsign.upper()
        self.fbb_state['alerts']['calls_connected'].add(callsign)
        await self.bot_out_queue.put(f"Alert added for {callsign} connecting")

    async def check_alerts(self, message):
        ### TODO add better parsing of monitor output! parse callsign EXPLICITLY from the format etc, not just this basic "is in the string" thing
        for alert_call in self.fbb_state['alerts']['calls_seen']:
            if alert_call.encode('utf-8') in message:
                await self.bot_out_queue.put(f"ALERT: {alert_call} seen on air")  ## TODO add port info maybe? (by saving parsed portmap or using port config alias or whatever?)
        for alert_call in self.fbb_state['alerts']['calls_connected']:
            pass  ## TODO need proper parsing to see if connect flag to own callsign seen

    async def process_bot_incoming(self):
        try:
            while True:
                message = await self.bot_in_queue.get()
                message = message.rstrip()
                print(f"BpqInterface received: {message}")

                if self.telnet_in_queue is not None:
                    await self.handle_message_tenet_passthru(message)
                elif message == '#help' or message == "help" or message == "?":
                   await self.bot_out_queue.put("Commands:\n"
                                                "#help - this help message\n"
                                                "alert call seen - add an alert when a callsign is seen on any port set for monitoring.\n"
                                                "alert call connected - add an alert when a callsign connects to the node")  ## TODO flesh this out more
                elif message == 'telnet passthru':
                    telnet_in_queue = asyncio.Queue()  # Will be set to self.telnet_in_queue once logged in to telnet
                    self.telnet_passthru_task = asyncio.create_task(self.telnet_passthru(telnet_in_queue))
                elif message.startswith('alert'):
                    usage_alert = "Usage: alert <call> [alert_specific_args]"
                    if message.startswith('alert call'):
                        alert_call_usage = "Usage: alert call <seen|connected> <callsign>"
                        fields = message.split(' ')
                        if len(fields) >= 4:
                            callsign = fields[3]
                            if message.startswith('alert call seen'):
                                await self.add_alert_call_seen(callsign)
                            elif message.startswith('alert call connected'):
                                await self.add_alert_call_connected(callsign)
                            else:
                                await self.bot_out_queue.put(alert_call_usage)
                        else:
                            await self.bot_out_queue.put(alert_call_usage)
                    else:
                        await self.bot_out_queue.put(usage_alert)
                else:
                   await self.bot_out_queue.put("Unknown command: type #help for help")

                self.bot_in_queue.task_done()
        except Exception as e:
            print(e)
    
    async def handle_message_tenet_passthru(self, message):
        if message == '#quit':
            self.telnet_passthru_task.cancel()
        else:
            await self.telnet_in_queue.put(message)

    async def keepalive_nulls(self, writer, interval_secs=540):
        while True:
            await asyncio.sleep(interval_secs)
            writer.write(b"\x00")
            await writer.drain()

    async def fbb_start_monitor(self):
        await self.ensure_fbb_connected()
        self.fbb_state['monitoring'] = True
        self.fbb_writer.write(b"\\\\\\\\7 1 1 1 1 0 0 1\r")  ## TODO hardcoded portmap here enabling the first few ports, get from config or whatever later
        await self.fbb_writer.drain()

    async def ensure_fbb_connected(self):
        try:
            if self.fbb_writer is None:
                keepalive = None
                try:
                        self.fbb_reader, self.fbb_writer = await asyncio.open_connection('127.0.0.1', 8011)
                except ConnectionRefusedError:
                    print("Could not connect to FBB")
                    await self.bot_out_queue.put("Could not connect to FBB - exiting")  ## TODO prob need retry mechanism for FBB - also this may NOT go to the bot directly in future as its sort of internals!
                    raise

                keepalive = asyncio.create_task(self.keepalive_nulls(self.fbb_writer))

                self.fbb_writer.write(b"2E0HKD\rR@dio\rBPQTERMTCP\r")
                await self.fbb_writer.drain()

                if self.fbb_connection_task is None:
                    self.fbb_connection_task = asyncio.create_task(self.fbb_connection())

        except Exception as e:
            print(f"ERROR in ensure_fbb_connected(): {e}")
            if keepalive is not None:
                keepalive.cancel()
            if self.fbb_writer is not None:
                self.fbb_writer.close()
                self.fbb_writer = None
            print("FBB connection terminated")
            ### TODO re-raise here, so fbb_start_monitor() knows not to set monitorring == True??
            ### AND if self.fbb_state['monitoring'] == True then ATTEMPT RECONNECT! And drop a message to the user saying connection lost and that alerts will not happen until reconnect.. then message on successful reconnection

    async def fbb_connection(self):
        try:
            while True:
                byte = await self.fbb_reader.read(1)
                if len(byte) == 0:
                    next
                elif byte[0] == 0xff:
                    # Monitor output, see if it is a portmap or an actual monitored packet
                    byte = await self.fbb_reader.read(1)
                    while(len(byte) == 0):
                        byte = await self.fbb_reader.read(1)
                    if byte[0] == 0xff:
                        message = await self.fbb_reader.readuntil(b'|')
                        message = message[:-1]  # Remove the trailing '|'
                        try:
                            port_count = int(message)
                            ports = []
                            for i in range(0, port_count):
                                message = await self.fbb_reader.readuntil(b'|')
                                ports.append(message[:-1])
                            print(f"FBB monitor portmap received: port count: {port_count}, ports: {ports}")
                        except Exception as e:
                            print("FBB error parsing portmap: {e}")
                    elif byte[0] == 0x1b:
                        byte = await self.fbb_reader.read(1)
                        while(len(byte) == 0):
                            byte = await self.fbb_reader.read(1)
                        if byte[0] == 0x11:
                            message = await self.fbb_reader.readuntil(b'\xfe')
                            message = message[:-1]  # Remove the trailing \xfe
                            print(f"FBB monitor received: {message}")

                            if self.fbb_state['monitoring']:
                                await self.check_alerts(message)

                        else:
                            print(f"FBB unrecognised byte following a message starting with 0xff 0x1b, expected 0x11 for a monitor message, got: {byte}. A small amount of junk may now be recieved until the end of this unknown message.")
                    else:
                        print(f"FBB unrecognised byte following a message starting with 0xff, expected 0x1b or 0xff for a monitor message, got: {byte}. A small amount of junk may now be recieved until the end of this unknown message.")
                else:
                    # Non-monitor output
                    message = await self.fbb_reader.readuntil(b'\r')
                    message = byte + message[:-1]  # Add on the first byte received originally, and remove trailing \r
                    print(f"FBB non-monitor received: {message}")  ## TODO interpret it based on other state and pass it where it may need to go
                    if message == b'Connected to TelnetServer':
                        pass

        except Exception as e:
            print(f"Error in fbb_connection(): {e}")

    # This looks a lot like asyncio.readuntil() or asyncio.readline() but we implement it here so we can get a partial
    # read buffer even if no separator was seen after a certain timeout, see: async_read(). Additionally, it allows
    # reading a larger buffer in one go even if it includes several separators, which can be more efficient and makes it
    # easier if forwarding to the bot to send one big chunk rather than lots of individual chunks/lines that can be rate
    # limited.
    async def asyncio_readuntil_or_partial(self, reader, buffer, separator=b'\n'):
        got_newline = False
        while not got_newline:
            bytes = await reader.read(10000)
            buffer.write(bytes)
            if buffer.getvalue().endswith(separator):
                got_newline = True

    # Reads until a newline, or there is a timeout, in which case return whatever was already read into the buffer.
    # This means we will not garble most messages that do end in a newline, but if there is one that does not (like a
    # prompt) it will still get sent back to the user.
    async def async_read(self, reader, timeout=5, decode=True, separator=b'\n'):
        buffer = BytesIO()
        try:
            await asyncio.wait_for(self.asyncio_readuntil_or_partial(reader, buffer, separator=separator), timeout=timeout)
        except asyncio.exceptions.TimeoutError:
            pass  # If we timeout, just send whatever we have in the buffer even though it does not end in a newline
        message = buffer.getvalue()
        if decode:
            message = message.decode('utf-8', 'ignore')
        return message

    # TODO handle remote-disconnect
    async def telnet_passthru(self, telnet_in_queue):
        try:
            await self.bot_out_queue.put("Entering telnet passthru mode, all further messages will be sent directly to a logged in telnet session. To exit telnet passthru send: #quit")

            try:
                telnet_reader, telnet_writer = await asyncio.open_connection('127.0.0.1', 8010)
            except ConnectionRefusedError:
                await self.bot_out_queue.put("Could not connect to telnet - exiting telnet passthru mode")
                return

            keepalive = asyncio.create_task(self.keepalive_nulls(telnet_writer))

            # Wait for user prompt
            try:
                await asyncio.wait_for(telnet_reader.readuntil(b':'), timeout=5)
            except asyncio.exceptions.TimeoutError:
                print("The long operation timed out, but we've handled it.")  ## TODO probably bail, allow reconnect later or whatever - maybe send error to bot?
            except (asyncio.LimitOverrunError, asyncio.IncompleteReadError) as e:
                print(f"asyncio error waiting for user prompt: {e}")  ## TODO probably bail, allow reconnect later or whatever - maybe send error to bot?

            telnet_writer.write("2E0HKD\r".encode('utf-8'))
            await telnet_writer.drain()

            # Wait for pass prompt
            try:
                await asyncio.wait_for(telnet_reader.readuntil(b':'), timeout=5)
            except asyncio.exceptions.TimeoutError:
                print("The long operation timed out, but we've handled it.")  ## TODO probably bail, allow reconnect later or whatever - maybe send error to bot?
            except (asyncio.LimitOverrunError, asyncio.IncompleteReadError) as e:
                print(f"asyncio error waiting for user prompt: {e}")  ## TODO probably bail, allow reconnect later or whatever - maybe send error to bot?

            ### TODO add a timeout to writes as well? Maybe wrap into a method
            telnet_writer.write("R@dio\r".encode('utf-8'))
            await telnet_writer.drain()

            # Now we are connected and logged in, we can start accepting telnet input via the bot, setting
            # self.telnet_in_queue will cause process_bot_incoming() to make use of it
            self.telnet_in_queue = telnet_in_queue
            asyncio.create_task(self.telnet_passthru_outgoing(telnet_writer))

            while True:
                message = await self.async_read(telnet_reader)
                if len(message.rstrip()) > 0:
                    print(f"telnet received: {message}")
                    await self.bot_out_queue.put(message)
        except asyncio.CancelledError:
            telnet_writer.write("b\r".encode('utf-8'))
            try:
                await asyncio.wait_for(await telnet_writer.drain(), timeout=5)
                # Last chance read for whatever is sent as we're quiting telnet
                message = await self.async_read(telnet_reader)
                if len(message.rstrip()) > 0:
                    await self.bot_out_queue.put(message)
            except (asyncio.exceptions.TimeoutError, asyncio.LimitOverrunError, asyncio.IncompleteReadError) as e:
                pass  # Oh well, we're quitting
            raise  # asyncio.CancelledError expects to be propogated after cleanup
        finally:
            keepalive.cancel()
            telnet_writer.close()
            await self.bot_out_queue.put("Telnet passthru terminated")
            self.telnet_in_queue = None  ## TODO also ideally do this if the remote end closes the telnet connection, and in that case also send a message via the bot to let the user know what happened

    async def telnet_passthru_outgoing(self, telnet_writer):
        try:
            while True:
                message = await self.telnet_in_queue.get()
                telnet_writer.write(f"{message}\r".encode('utf-8'))
                await telnet_writer.drain()
                print(f"Telnet sent: {message}")
                self.telnet_in_queue.task_done()
        except Exception as e:
            print(e)


async def main():
    try:
        with open('packetnodebot.yaml', 'r') as file:
            conf = yaml.safe_load(file)

        bot_in_queue = asyncio.Queue()
        bot_out_queue = asyncio.Queue()

        bpq = BpqInterface(bot_in_queue, bot_out_queue)

        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        discord_con = packetnodebot.discord.DiscordConnector(bot_in_queue=bot_in_queue, bot_out_queue=bot_out_queue, intents=intents)

        discord_task = asyncio.create_task(discord_con.start(conf['discord']['token']))
        bpq_process_bot_in_task = asyncio.create_task(bpq.process_bot_incoming())
        discord_process_bot_out_task = asyncio.create_task(discord_con.process_bot_outgoing())

        await asyncio.gather(discord_task, bpq_process_bot_in_task, discord_process_bot_out_task)#, fbb_connection_task)  ## TODO only addng FBB task here to test it

    except Exception as e:
        print(e)

def bpqnodebot():
    try:
        asyncio.run(main())
    except Exception as e:
        print(e)
