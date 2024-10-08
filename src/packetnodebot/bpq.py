import traceback
import asyncio
import yaml
from io import BytesIO
import discord
import packetnodebot.common
import packetnodebot.discord


def portnum_bin(port_num):
    return 0b1 << (int(port_num) - 1)


class BpqInterface():
    COMMANDS_FIXED = ("Commands:\n"
                      "help                                 - This help message.\n"
                      "fixed <on|off>                       - Turn fixed-width font mode on/off.\n"
                      "telnet                               - Connect to telnet, all messages recieved will be sent directly to telnet. Use #quit to end the telnet session.\n"
                      "monitor <on|off>                     - Monitor all configured ports for any packets seen.\n"
                      "monports <add|del> <portnum>         - Add/delete a port number from monitoring.\n"
                      "monfilter <add|del> <from|to> <call> - Add/delete calls to exclude from monitoring.\n"
                      "alert call seen                      - Add an alert when a callsign is seen on any port set for monitoring.\n"
                      "alert call connected                 - Add an alert when a callsign connects to the node.\n"
                      "remove alert                         - Remove any of the above alerts\n"
                      "terminate bot                        - Shut down the bot, you will no longer be able to interact with it until it is restarted.")
    COMMANDS = ("Commands:\n"
                "help - This help message.\n"
                "fixed <on|off> - Turn fixed-width font mode on/off.\n"
                "telnet - Connect to telnet, all messages recieved will be sent directly to telnet. Use #quit to end the telnet session.\n"
                "monitor <on|off> - Monitor all configured ports for any packets seen.\n"
                "monports <add|del> <portnum> - Add/delete a port number from monitoring.\n"
                "monfilter <add|del> <from|to> <call> - Add/delete calls to exclude from monitoring.\n"
                "alert call seen - Add an alert when a callsign is seen on any port set for monitoring.\n"
                "alert call connected - Add an alert when a callsign connects to the node.\n"
                "remove alert - Remove any of the above alerts\n"
                "terminate bot - Shut down the bot, you will no longer be able to interact with it until it is restarted.")

    def __init__(self, conf, bot_in_queue, bot_out_queue, terminated):
        self.conf = conf
        self.bot_in_queue = bot_in_queue
        self.bot_out_queue = bot_out_queue
        self.terminated = terminated
        self.telnet_passthru_task = None
        self.telnet_in_queue = None  # Used for telnet passthru, set to an asyncio.Queue when in use

        self.command_state = None
        self.fbb_state = {
            'monitoring': False,  # True if anything requiring FBB monitor data is enabled, so FBB connection is kept
            'bot_monitor': False,
            'alerts': {
                'calls_seen': set(),
                'calls_connected': set()
            }
        }
        self.fbb_connection_task = None
        self.fbb_writer = None
        self.fbb_reader = None
        if 'monitor_ports' in self.conf['bpq']:
            self.set_monitor_ports(self.conf['bpq']['monitor_ports'])
        else:
            self.set_monitor_ports([])

        if 'mon_filter' in self.conf['bpq']:
            self.mon_filter = self.conf['bpq']['mon_filter']
        else:
            self.mon_filter = {}
        if 'from' not in self.mon_filter:
            self.mon_filter['from'] = []
        if 'to' not in self.mon_filter:
            self.mon_filter['to'] = []
        self.mon_filter['from'] = [s.upper() for s in self.mon_filter['from']]
        self.mon_filter['to'] = [s.upper() for s in self.mon_filter['to']]

        if 'fixed_width_font' in conf and conf['fixed_width_font']:
            self.fixed_width = True
        else:
            self.fixed_width = False

        if 'monitor_on_startup' in self.conf['bpq'] and self.conf['bpq']['monitor_on_startup']:
            asyncio.create_task(self.start_bot_monitor())

        if 'telnet_custom_prompt_end' in self.conf['bpq']:
            self.telnet_prompt_end = self.conf['bpq']['telnet_custom_prompt_end'].encode('utf-8')
        else:
            self.telnet_prompt_end = b':'

    async def start_bot_monitor(self):
        await self.fbb_start_monitor()
        self.fbb_state['bot_monitor'] = True

    def set_monitor_ports(self, ports):
        self.monitor_ports = ports
        self.monitor_ports_bin = 0b0
        for port in self.monitor_ports:
            self.monitor_ports_bin |= portnum_bin(port)

    def passes_monitor_filter(self, message):
        if len(self.mon_filter['from']) == 0 and len(self.mon_filter['to']) == 0:
            return True
        first_space = message.find(' ')
        if first_space == -1:
            print(f"Failed to parse from/to in passes_monitor_filter() for message: {message}")
            return True  # Pass the message just in case
        second_space = message.find(' ', first_space + 1)
        if second_space == -1:
            print(f"Failed to parse from/to in passes_monitor_filter() for message: {message}")
            return True  # Pass the message just in case
        from_to = message[first_space + 1:second_space]
        from_to = from_to.split('>')
        if len(from_to) < 2:
            print(f"Failed to parse from/to in passes_monitor_filter() for message: {message}")
            return True  # Pass the message just in case
        from_field = from_to[0]
        to_field = from_to[1]
        from_calls = from_field.split(',')
        for from_call in from_calls:
            if from_call.upper() in self.mon_filter['from']:
                return False
        to_calls = to_field.split(',')
        for to_call in to_calls:
            if to_call.upper() in self.mon_filter['to']:
                return False
        return True

    def fmt_monfilter_from(self):
        if len(self.mon_filter['from']) > 0:
            return ', '.join(self.mon_filter['from'])
        else:
            return '(none)'

    def fmt_monfilter_to(self):
        if len(self.mon_filter['to']) > 0:
            return ', '.join(self.mon_filter['to'])
        else:
            return '(none)'

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

    async def remove_alert_call_seen(self, callsign):
        callsign = callsign.upper()
        if callsign in self.fbb_state['alerts']['calls_seen']:
            self.fbb_state['alerts']['calls_seen'].remove(callsign)
            await self.bot_out_queue.put(f"Alert removed for {callsign} seen on air")
        else:
            await self.bot_out_queue.put(f"There was no alert for {callsign} seen on air")
        await self.stop_fbb_monitor_if_not_required()

    async def remove_alert_call_connected(self, callsign):
        callsign = callsign.upper()
        if callsign in self.fbb_state['alerts']['calls_connected']:
            self.fbb_state['alerts']['calls_connected'].remove(callsign)
            await self.bot_out_queue.put(f"Alert removed for {callsign} connecting")
        else:
            await self.bot_out_queue.put(f"There was no alert for {callsign} connecting")
        await self.stop_fbb_monitor_if_not_required()

    async def stop_fbb_monitor_if_not_required(self):
        if (len(self.fbb_state['alerts']['calls_connected']) == 0 and
            len(self.fbb_state['alerts']['calls_seen']) == 0 and not self.fbb_state['bot_monitor']):  ### TODO also check we are connected to FBB, if not, obviously do nothing
            self.fbb_writer.write(b"\\\\\\\\0 0 0 0 0 0 0 0\r")
            await self.fbb_writer.drain()
            self.fbb_state['monitoring'] = False
            print("No more alerts/monitoring so FBB monitoring stopped")
        ### TODO disconnect FBB if nothong else needs the connection?

    async def check_alerts(self, message):

        ### TODO also probably need an ALERT DUPLICATE TIMER of some kind, so as to not SPAM people with alerts close together
        ### maybe do not fire an alert again for 5 mins or something - configurable of course but have a sane default

        ### TODO add better parsing of monitor output! parse callsign EXPLICITLY from the format etc, not just this basic "is in the string" thing
        for alert_call in self.fbb_state['alerts']['calls_seen']:
            if alert_call.encode('utf-8') in message:
                await self.bot_out_queue.put(f"ALERT: {alert_call} seen on air")  ## TODO add port info maybe? (by saving parsed portmap or using port config alias or whatever?)
        for alert_call in self.fbb_state['alerts']['calls_connected']:
            pass  ## TODO need proper parsing to see if connect flag to own callsign seen

    async def process_bot_incoming(self):
        while not self.terminated.is_set():
            message = await self.bot_in_queue.get()
            message = message.lower()
            try:
                message = message.rstrip()
                if self.telnet_in_queue is not None:
                    await self.handle_message_tenet_passthru(message)
                elif self.command_state == 'terminate_bot_confirm':
                    if message.lower() == 'yes':
                        await self.bot_out_queue.put("Bot Terminating - bye!")
                        self.terminated.set()
                    else:
                        self.command_state = None
                        await self.bot_out_queue.put("Bot Terminate aborted")
                elif message == '#help' or message == "help" or message == "?":
                   if self.fixed_width:
                       await self.bot_out_queue.put(BpqInterface.COMMANDS_FIXED)
                   else:
                       await self.bot_out_queue.put(BpqInterface.COMMANDS)
                elif message.startswith("fixed"):
                    fixed_usage = "Usage: fixed <on|off>"
                    fields = message.split(' ')
                    if len(fields) == 2 and (fields[1] == "on" or fields[1] == "off"):
                        if fields[1] == "on":
                            self.fixed_width = True
                            await self.bot_out_queue.put(packetnodebot.common.InternalBotCommand('fixed', 'on'))
                            await self.bot_out_queue.put("Fixed-width font enabled")
                        else:
                            self.fixed_width = False
                            await self.bot_out_queue.put(packetnodebot.common.InternalBotCommand('fixed', 'off'))
                            await self.bot_out_queue.put("Fixed-width font disabled")
                    else:
                        await self.bot_out_queue.put(fixed_usage)
                elif message == 'telnet':   ### TODO maybe add an option to use an alternative telnet user than the default configured
                    telnet_in_queue = asyncio.Queue()  # Will be set to self.telnet_in_queue once logged in to telnet
                    self.telnet_passthru_task = asyncio.create_task(self.telnet_passthru(telnet_in_queue))
                elif message.startswith('monports'):
                    monports_usage = "Usage: monports <add|del> <portnum>"
                    fields = message.split(' ')
                    if len(fields) == 1:
                        await self.bot_out_queue.put("Monitor set to use ports: "
                                                     f"{', '.join(str(port) for port in self.monitor_ports)}\n"
                                                     f"{monports_usage}")
                    elif len(fields) == 3:
                        try:
                            if fields[1] == 'add':
                                port = int(fields[2])
                                if port not in self.monitor_ports:
                                    self.monitor_ports.append(port)
                                    self.set_monitor_ports(self.monitor_ports)
                                    if self.fbb_state['monitoring']:  # If already monitoring, update the active ports
                                        self.fbb_writer.write(f"\\\\\\\\{self.monitor_ports_bin} 1 1 1 0 0 0 1\r".encode('utf-8'))
                                        await self.fbb_writer.drain()
                                await self.bot_out_queue.put("Monitor set to use ports: "
                                                            f"{', '.join(str(port) for port in self.monitor_ports)}")
                            elif fields[1] == 'del':
                                port = int(fields[2])
                                if port in self.monitor_ports:
                                    self.monitor_ports.remove(port)
                                    self.set_monitor_ports(self.monitor_ports)
                                    if self.fbb_state['monitoring']:  # If already monitoring, update the active ports
                                        self.fbb_writer.write(f"\\\\\\\\{self.monitor_ports_bin} 1 1 1 0 0 0 1\r".encode('utf-8'))
                                        await self.fbb_writer.drain()
                                await self.bot_out_queue.put("Monitor set to use ports: "
                                                            f"{', '.join(str(port) for port in self.monitor_ports)}")
                            else:
                                await self.bot_out_queue.put(monports_usage)
                        except (TypeError, ValueError):
                            await self.bot_out_queue.put(monports_usage)  # Probably a non-number port provided
                    else:
                        await self.bot_out_queue.put(monports_usage)
                elif message.startswith('monfilter'):
                    monfilter_usage = "Usage: monfilter <add|del> <from|to> <call>"
                    fields = message.split(' ')
                    if len(fields) == 1:
                        await self.bot_out_queue.put(f"Monitor filtering From calls: {self.fmt_monfilter_from()}, "
                                                     f"To calls: {self.fmt_monfilter_to()}\n{monfilter_usage}")
                    elif len(fields) == 4 and (fields[1] == 'add' or fields[1] == 'del') and (fields[2] == 'from' or
                                                                                              fields[2] == 'to'):
                        call = fields[3].upper()
                        if fields[1] == 'add':
                            if call not in self.mon_filter[fields[2]]:
                                self.mon_filter[fields[2]].append(call)
                        elif fields[1] == 'del':
                            if call in self.mon_filter[fields[2]]:
                                self.mon_filter[fields[2]].remove(call)
                        await self.bot_out_queue.put(f"Monitor filtering From calls: {self.fmt_monfilter_from()}, "
                                                     f"To calls: {self.fmt_monfilter_to()}")
                    else:
                        await self.bot_out_queue.put(monfilter_usage)
                elif message.startswith('monitor'):
                    monitor_usage = "Usage: monitor <on|off>"
                    fields = message.split(' ')
                    if len(fields) == 2:
                        if fields[1] == 'on':
                            if not self.fbb_state['monitoring']:
                                await self.fbb_start_monitor()
                            self.fbb_state['bot_monitor'] = True
                            await self.bot_out_queue.put("Monitor on")
                        elif fields[1] == 'off':
                            self.fbb_state['bot_monitor'] = False
                            #if self.fbb_task is not None:
                            #    self.fbb_task.cancel()
                            await self.stop_fbb_monitor_if_not_required()
                            await self.bot_out_queue.put("Monitor off")
                        else:
                            await self.bot_out_queue.put(monitor_usage)
                    else:
                        if self.fbb_state['bot_monitor']:
                            await self.bot_out_queue.put(f"Monitor is on\n{monitor_usage}")
                        else:
                            await self.bot_out_queue.put(f"Monitor is off\n{monitor_usage}")
                elif message.startswith('alert'):  ## TODO optionally specify only certain ports to look for the call on?
                    usage_alert = "Usage: alert <call> [alert_specific_args]"
                    if message.startswith('alert call'):
                        alert_call_usage = "Usage: alert call <seen|connected> <callsign>"
                        fields = message.split(' ')
                        if len(fields) == 4:
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
                elif message.startswith('remove alert'):
                    usage_alert = "Usage: remove alert <call> [alert_specific_args]"
                    if message.startswith('remove alert call'):
                        alert_call_usage = "Usage: remove alert call <seen|connected> <callsign>"
                        fields = message.split(' ')
                        if len(fields) == 5:
                            callsign = fields[4]
                            if message.startswith('remove alert call seen'):
                                await self.remove_alert_call_seen(callsign)
                            elif message.startswith('remove alert call connected'):
                                await self.remove_alert_call_connected(callsign)
                            else:
                                await self.bot_out_queue.put(alert_call_usage)
                        else:
                            await self.bot_out_queue.put(alert_call_usage)
                    else:
                        await self.bot_out_queue.put(usage_alert)
                elif message == 'terminate bot':
                    await self.bot_out_queue.put("Terminate Bot - Are you sure? You will not be able to interact with "
                                                 "the bot until you restart it on the node. Reply 'yes' to confirm.") 
                    self.command_state = 'terminate_bot_confirm'
                else:
                   await self.bot_out_queue.put("Unknown command: type help for help")
            except Exception as e:
                tb = traceback.format_exc()
                print(f"Error in process_bot_incoming(): {tb}")
            finally:
                self.bot_in_queue.task_done()
    
    async def handle_message_tenet_passthru(self, message):
        if message == '#quit':
            self.telnet_passthru_task.cancel()
        else:
            await self.telnet_in_queue.put(message)

    async def keepalive_nulls(self, writer, interval_secs=540):
        while not self.terminated.is_set():
            try:
                await asyncio.sleep(interval_secs)
                writer.write(b"\x00")
                await writer.drain()
            except Exception as e:
                print(f"Error in keepalive_nulls(): {e}. Keepalive attempts will still continue to be sent.")

    async def fbb_start_monitor(self):
        await self.ensure_fbb_connected()
        self.fbb_state['monitoring'] = True
        self.fbb_writer.write(f"\\\\\\\\{self.monitor_ports_bin} 1 1 1 0 0 0 1\r".encode('utf-8'))
        await self.fbb_writer.drain()

    async def ensure_fbb_connected(self):
        if ('fbb_host' not in self.conf['bpq'] or 'fbb_port' not in self.conf['bpq'] or
           'fbb_user' not in self.conf['bpq'] or 'fbb_pass' not in self.conf['bpq']):
            await self.bot_out_queue.put("Missing fbb config options under 'bpq;, expected: fbb_host, "
                                         "fbb_port, fbb_user, fbb_pass")
            raise Exception("FBB creds not configured")
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

                self.fbb_writer.write(f"{self.conf['bpq']['fbb_user']}\r{self.conf['bpq']['fbb_pass']}\r"
                                      "BPQTERMTCP\r\\\\\\\\0 0 0 0 0 0 0 0\r".encode('utf-8'))
                await self.fbb_writer.drain()

                try:
                    # We sent a no-monitor setting above, we only expect a connected message ending in "\r" and not a
                    # portmap ending in "|"
                    message = await self.async_read(self.fbb_reader, timeout=5, decode=True, separator=b'\r')
                    if message == 'password:':
                        await self.bot_out_queue.put("Monitor could not be enabled because the FBB user/pass was not "
                                                     "accepted")
                        raise Exception("Bad FBB creds")
                except asyncio.exceptions.TimeoutError:
                    await self.bot_out_queue.put("Monitor could not be enabled due to no response from FBB, is the "
                                                 "user/pass set correctly?")
                    raise Exception("Bad FBB creds")

                if self.fbb_connection_task is None:
                    self.fbb_connection_task = asyncio.create_task(self.fbb_connection())

        except Exception as e:

            ### TODO this cleanup stuff probably also needs to happen in fbb_connection - but it wont have "keepalive", maybe pass it as an arg to the task (as seems overkill to make an instance var)
            ### Then we can replicate this cleanup in a finally in fbb_connection (here it only needs to be in exception handler though)

            print(f"ERROR in ensure_fbb_connected(): {e}")
            if keepalive is not None:
                keepalive.cancel()
            if self.fbb_writer is not None:
                self.fbb_writer.close()
                self.fbb_writer = None
            print("FBB connection terminated")
            raise
            ### TODO re-raise here, so fbb_start_monitor() knows not to set monitorring == True??
            ### AND if self.fbb_state['monitoring'] == True then ATTEMPT RECONNECT! And drop a message to the user saying connection lost and that alerts will not happen until reconnect.. then message on successful reconnection

    async def fbb_connection(self):
        try:
            while not self.terminated.is_set():
                byte = await self.fbb_reader.read(1)
                if len(byte) == 0:
                    await self.bot_out_queue.put("Lost FBB connection, alerts may not fire until connection is re-established")  ## TODO we'll want to auto-reconnect if there is monitoring active - keep retrying with delay until monitoring == False (eg. from user commands removing alerts)
                    break  # EOF, ie. remove end disconnected
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
                        if byte[0] == 0x11 or byte[0] == 0x5b:  # These are terminal colour codes
                            message = await self.fbb_reader.readuntil(b'\xfe')
                            message = message[:-1]  # Remove the trailing \xfe
                            if self.fbb_state['bot_monitor']:  ## TODO should this go in check_alerts(), and if so should we rename the method to be more generic?
                                message_str = packetnodebot.common.bytes_str(message)
                                if self.passes_monitor_filter(message_str):
                                    await self.bot_out_queue.put(f"Monitor: {message_str}")
                            if self.fbb_state['monitoring']:
                                await self.check_alerts(message)
                        else:
                            print("FBB unrecognised byte following a message starting with 0xff 0x1b, expected 0x11 "
                                  f"for a monitor message, got: {byte}. A small amount of junk may now be recieved "
                                  "until the end of this unknown message.")
                    else:
                        print("FBB unrecognised byte following a message starting with 0xff, expected 0x1b or 0xff "
                              f"for a monitor message, got: {byte}. A small amount of junk may now be recieved until "
                              "the end of this unknown message.")
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
    async def async_read(self, reader, timeout=10, decode=True, separator=b'\n'):
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
        if ('telnet_host' not in self.conf['bpq'] or 'telnet_port' not in self.conf['bpq'] or
           'telnet_user' not in self.conf['bpq'] or 'telnet_pass' not in self.conf['bpq']):
            await self.bot_out_queue.put("Missing telnet config options under 'bpq;, expected: telnet_host, "
                                         "telnet_port, telnet_user, telnet_pass")
            return
        try:
            await self.bot_out_queue.put("Entering telnet passthru mode, all further messages will be sent directly to "
                                         "a logged in telnet session. To exit telnet passthru send: #quit")

            try:
                telnet_reader, telnet_writer = await asyncio.open_connection(self.conf['bpq']['telnet_host'],
                                                                             self.conf['bpq']['telnet_port'])
            except ConnectionRefusedError:
                await self.bot_out_queue.put("Could not connect to telnet - exiting telnet passthru mode")
                return

            keepalive = asyncio.create_task(self.keepalive_nulls(telnet_writer))

            # Wait for user prompt
            try:
                await asyncio.wait_for(telnet_reader.readuntil(self.telnet_prompt_end), timeout=5)
            except asyncio.exceptions.TimeoutError:
                print("The long operation timed out, but we've handled it.")  ## TODO probably bail, allow reconnect later or whatever - maybe send error to bot?
            except (asyncio.LimitOverrunError, asyncio.IncompleteReadError) as e:
                print(f"asyncio error waiting for user prompt: {e}")  ## TODO probably bail, allow reconnect later or whatever - maybe send error to bot?

            telnet_writer.write(f"{self.conf['bpq']['telnet_user']}\r".encode('utf-8'))
            await telnet_writer.drain()

            # Wait for pass prompt
            try:
                await asyncio.wait_for(telnet_reader.readuntil(self.telnet_prompt_end), timeout=5)
            except asyncio.exceptions.TimeoutError:
                print("The long operation timed out, but we've handled it.")  ## TODO probably bail, allow reconnect later or whatever - maybe send error to bot?
            except (asyncio.LimitOverrunError, asyncio.IncompleteReadError) as e:
                print(f"asyncio error waiting for user prompt: {e}")  ## TODO probably bail, allow reconnect later or whatever - maybe send error to bot?

            telnet_writer.write(f"{self.conf['bpq']['telnet_pass']}\r".encode('utf-8'))
            await telnet_writer.drain()

            # Now we are connected and logged in, we can start accepting telnet input via the bot, setting
            # self.telnet_in_queue will cause process_bot_incoming() to make use of it
            self.telnet_in_queue = telnet_in_queue
            asyncio.create_task(self.telnet_passthru_outgoing(telnet_writer))

            while not self.terminated.is_set():
                try:
                    message = await self.async_read(telnet_reader)
                    if len(message.rstrip()) > 0:
                        #print(f"telnet received: {message}")
                        await self.bot_out_queue.put(message)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    print(f"Error in telnet_passthru() during async_read loop: {e}")
                    try:
                        await self.bot_out_queue.put(message)
                    except:
                        pass
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
        while not self.terminated.is_set():
            try:
                message = await self.telnet_in_queue.get()
                telnet_writer.write(f"{message}\r".encode('utf-8'))
                await telnet_writer.drain()
                #print(f"Telnet sent: {message}")
            except Exception as e:
                print(f"Error in telnet_passthru_outgoing {e}")
            finally:
                self.telnet_in_queue.task_done()


async def main(terminated):
    try:
        with open('packetnodebot.yaml', 'r') as file:
            conf = yaml.safe_load(file)
        if 'bot_connector' not in conf:
            exit('Missing bot_connector in config file')
        if conf['bot_connector'] not in conf:
            exit(f"Missing connector {conf['bot_connector']} in config file")
        if 'bpq' not in conf:
            exit('Missing bpq in config file')

        bot_in_queue = asyncio.Queue()
        bot_out_queue = asyncio.Queue()
        bpq = BpqInterface(conf, bot_in_queue, bot_out_queue, terminated)

        if conf['bot_connector'] == 'discord':
            intents = discord.Intents.default()
            intents.message_content = True
            intents.members = True
            bot_connector = packetnodebot.discord.DiscordConnector(conf=conf, conf_file='packetnodebot.yaml',
                                                                   terminated=terminated, bot_in_queue=bot_in_queue,
                                                                   bot_out_queue=bot_out_queue, intents=intents)
            connector_task = asyncio.create_task(bot_connector.start(conf['discord']['token']))
        else:
            exit('Unsupported bot_connector')

        bpq_process_bot_in_task = asyncio.create_task(bpq.process_bot_incoming())
        process_bot_out_task = asyncio.create_task(bot_connector.process_bot_outgoing())
        await asyncio.gather(connector_task, bpq_process_bot_in_task, process_bot_out_task)

        print("GATHERED TASKS ALL ENDED")

    except Exception as e:
        print(f"Error in main(): {e}")


def bpqnodebot():
    try:
        terminated = asyncio.Event()
        asyncio.run(main(terminated))
    except KeyboardInterrupt:
        terminated.set()
    except Exception as e:
        print(f"Error in bpqnodebot(): {e}")
