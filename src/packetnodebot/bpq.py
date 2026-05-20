import traceback
import asyncio
import socket
import time
import yaml
from io import BytesIO
import discord
import packetnodebot.common
import packetnodebot.discord


def portnum_bin(port_num):
    return 0b1 << (int(port_num) - 1)


def parse_monitor_frame(message_str):
    """Parse a decoded BPQ monitor frame string.

    Expected format: '<port_field> <from>to[,via1,via2,...]> <frame_type ...>[: payload]'
    e.g. '1:Port1 N0CALL>OTHER,RELAY* <SABM C P>: hello'

    Returns dict with keys: port (int|None), port_label (str|None), from (str),
    to (str), via (list[str]), frame_type (str|None). Returns None if the frame
    can't be parsed — callers should fall back or skip.
    """
    try:
        lt = message_str.find('<')
        if lt == -1:
            return None
        gt = message_str.find('>', lt)
        if gt == -1:
            return None
        addr_section = message_str[:lt].rstrip()
        frame_type = message_str[lt + 1:gt]

        tokens = addr_section.split()
        if len(tokens) < 2:
            return None

        port_field = tokens[0]
        port = None
        port_label = None
        if ':' in port_field:
            port_num_str, _, port_label = port_field.partition(':')
        else:
            port_num_str = port_field
        digits = ''
        for c in port_num_str:
            if c.isdigit():
                digits += c
            else:
                break
        if digits:
            try:
                port = int(digits)
            except ValueError:
                port = None

        addr_parts = tokens[1].split('>')
        if len(addr_parts) < 2:
            return None
        from_call = addr_parts[0].upper()
        to_and_via = addr_parts[1].split(',')
        to_call = to_and_via[0].upper().rstrip('*')
        via_calls = [v.upper().rstrip('*') for v in to_and_via[1:]]

        return {
            'port': port,
            'port_label': port_label,
            'from': from_call,
            'to': to_call,
            'via': via_calls,
            'frame_type': frame_type,
        }
    except Exception:
        return None


class FbbAuthError(Exception):
    """Raised for FBB problems that won't be fixed by reconnecting (bad creds, missing config)."""
    pass


class BpqInterface():
    COMMANDS_FIXED = ("Commands:\n"
                      "help                                 - This help message.\n"
                      "fixed <on|off>                       - Turn fixed-width font mode on/off.\n"
                      "telnet                               - Connect to telnet, all messages recieved will be sent directly to telnet. Use #quit to end the telnet session.\n"
                      "hash_cmds_telnet <on|off>            - When on, while in a telnet session prefix any bot command with # (e.g. #fixed off) to run it instead of sending it to telnet.\n"
                      "monitor <on|off>                     - Monitor all configured ports for any packets seen.\n"
                      "monports <add|del> <portnum>         - Add/delete a port number from monitoring.\n"
                      "monfilter <add|del> <from|to> <call> - Add/delete calls to exclude from monitoring.\n"
                      "alert call seen                      - Add an alert when a callsign is seen on any port set for monitoring.\n"
                      "alert call connected                 - Add an alert when a callsign connects to the node.\n"
                      "alert cooldown <seconds>             - Suppress duplicate alerts for this many seconds (0 = disabled).\n"
                      "remove alert                         - Remove any of the above alerts\n"
                      "terminate bot                        - Shut down the bot, you will no longer be able to interact with it until it is restarted.")
    COMMANDS = ("Commands:\n"
                "help - This help message.\n"
                "fixed <on|off> - Turn fixed-width font mode on/off.\n"
                "telnet - Connect to telnet, all messages recieved will be sent directly to telnet. Use #quit to end the telnet session.\n"
                "hash_cmds_telnet <on|off> - When on, while in a telnet session prefix any bot command with # (e.g. #fixed off) to run it instead of sending it to telnet.\n"
                "monitor <on|off> - Monitor all configured ports for any packets seen.\n"
                "monports <add|del> <portnum> - Add/delete a port number from monitoring.\n"
                "monfilter <add|del> <from|to> <call> - Add/delete calls to exclude from monitoring.\n"
                "alert call seen - Add an alert when a callsign is seen on any port set for monitoring.\n"
                "alert call connected - Add an alert when a callsign connects to the node.\n"
                "alert cooldown <seconds> - Suppress duplicate alerts for this many seconds (0 = disabled).\n"
                "remove alert - Remove any of the above alerts\n"
                "terminate bot - Shut down the bot, you will no longer be able to interact with it until it is restarted.")

    def __init__(self, conf, bot_in_queue, bot_out_queue, terminated, state):
        self.conf = conf
        self.bot_in_queue = bot_in_queue
        self.bot_out_queue = bot_out_queue
        self.terminated = terminated
        self.state = state
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
        self.fbb_keepalive_task = None
        # True once the user has been told FBB is down; cleared again when we reconnect. Used to make
        # sure they only see one "down" message and one "back up" message per disconnect cycle, and
        # to drive the disconnect suffix on command responses.
        self.fbb_down_notified = False

        initial_delay = self.conf['bpq'].get('fbb_reconnect_initial_delay', 5)
        max_delay = self.conf['bpq'].get('fbb_reconnect_max_delay', 60)
        initial_delay = max(1, initial_delay)
        max_delay = max(initial_delay, max_delay)
        self.fbb_reconnect_initial_delay = initial_delay
        self.fbb_reconnect_max_delay = max_delay
        self.fbb_read_idle_timeout = max(1, self.conf['bpq'].get('fbb_read_idle_timeout', 600))

        self.node_callsigns = {c.upper() for c in self.conf.get('node_callsigns', [])}
        self.alert_cooldown = max(0, int(self.conf['bpq'].get('alert_cooldown', 300)))
        self.hash_cmds_telnet = bool(self.conf['bpq'].get('hash_cmds_telnet', False))
        self._alert_last_fired = {}  # {(alert_type, callsign): monotonic_seconds}

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

        if 'monitor_on_startup' in self.conf['bpq'] and self.conf['bpq']['monitor_on_startup']:
            asyncio.create_task(self.start_bot_monitor())

        self.command_tree = self._build_command_tree()

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
        parsed = parse_monitor_frame(message)
        if parsed is None:
            print(f"Failed to parse from/to in passes_monitor_filter() for message: {message}")
            return True  # Pass the message just in case
        if parsed['from'] in self.mon_filter['from']:
            return False
        if parsed['to'] in self.mon_filter['to']:
            return False
        for via in parsed['via']:
            if via in self.mon_filter['to']:
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
        await self.bot_out_queue.put(f"Alert added for {callsign} seen on air{self._fbb_connection_status_msg()}")

    async def add_alert_call_connected(self, callsign):
        if not self.fbb_state['monitoring']:
            await self.fbb_start_monitor()
        callsign = callsign.upper()
        self.fbb_state['alerts']['calls_connected'].add(callsign)
        await self.bot_out_queue.put(f"Alert added for {callsign} connecting{self._fbb_connection_status_msg()}")
        if not self.node_callsigns:
            await self.bot_out_queue.put("Note: node_callsigns not configured — 'connected' alerts will not fire.")

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
            len(self.fbb_state['alerts']['calls_seen']) == 0 and not self.fbb_state['bot_monitor']):
            self.fbb_state['monitoring'] = False
            self.fbb_down_notified = False
            await self._fbb_write_safe(b"\\\\\\\\0 0 0 0 0 0 0 0\r")
            print("No more alerts/monitoring so FBB monitoring stopped")
            if self.fbb_connection_task is not None:
                self.fbb_connection_task.cancel()
                try:
                    await self.fbb_connection_task
                except (asyncio.CancelledError, Exception):
                    pass
                self.fbb_connection_task = None
            self._teardown_fbb_socket()

    def _alert_allowed(self, alert_type, callsign):
        if self.alert_cooldown <= 0:
            return True
        key = (alert_type, callsign)
        now = time.monotonic()
        last = self._alert_last_fired.get(key)
        if last is None or (now - last) >= self.alert_cooldown:
            self._alert_last_fired[key] = now
            return True
        return False

    async def check_alerts(self, message):
        message_str = packetnodebot.common.bytes_str(message)
        parsed = parse_monitor_frame(message_str)
        if parsed is None:
            # Fall back to the old substring-against-raw-bytes behaviour so we still alert on weird
            # frames; we just lose port info and the false-positive guard for those.
            print(f"check_alerts(): could not parse frame, falling back to substring match: {message}")
            for alert_call in self.fbb_state['alerts']['calls_seen']:
                if alert_call.encode('utf-8') in message and self._alert_allowed('seen', alert_call):
                    await self.bot_out_queue.put(f"ALERT: {alert_call} seen on air")
            return

        addr_calls = {parsed['from'], parsed['to'], *parsed['via']}
        for alert_call in self.fbb_state['alerts']['calls_seen']:
            if alert_call in addr_calls and self._alert_allowed('seen', alert_call):
                port_suffix = f" on port {parsed['port']}" if parsed['port'] is not None else ""
                await self.bot_out_queue.put(f"ALERT: {alert_call} seen on air{port_suffix}")

        frame_type_first = parsed['frame_type'].split()[0] if parsed['frame_type'] else None
        if (frame_type_first == 'SABM' and parsed['to'] in self.node_callsigns
                and parsed['from'] in self.fbb_state['alerts']['calls_connected']
                and self._alert_allowed('connected', parsed['from'])):
            port_suffix = f" on port {parsed['port']}" if parsed['port'] is not None else ""
            await self.bot_out_queue.put(f"ALERT: {parsed['from']} connecting to {parsed['to']}{port_suffix}")

    async def process_bot_incoming(self):
        while not self.terminated.is_set():
            try:
                message = await asyncio.wait_for(self.bot_in_queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            try:
                message = message.rstrip()
                if self.telnet_in_queue is not None:
                    await self.handle_message_tenet_passthru(message)
                    continue
                message = message.lower()
                if self.command_state == 'terminate_bot_confirm':
                    if message == 'yes':
                        await self.bot_out_queue.put("Bot Terminating - bye!")
                        self.terminated.set()
                    else:
                        self.command_state = None
                        await self.bot_out_queue.put("Bot Terminate aborted")
                else:
                    await self._dispatch_command(message.split())
            except Exception as e:
                tb = traceback.format_exc()
                print(f"Error in process_bot_incoming(): {tb}")
            finally:
                self.bot_in_queue.task_done()

    # NOTE: when adding/renaming a user-facing command, also update COMMANDS and COMMANDS_FIXED above.
    def _build_command_tree(self):
        return {
            'help': self._cmd_help,
            '?': self._cmd_help,
            'fixed': self._cmd_fixed,
            'telnet': self._cmd_telnet,
            'hash_cmds_telnet': self._cmd_hash_cmds_telnet,
            'monports': self._cmd_monports,
            'monfilter': self._cmd_monfilter,
            'monitor': self._cmd_monitor,
            'alert': {
                'call': {
                    'seen': self._cmd_alert_call_seen,
                    'connected': self._cmd_alert_call_connected,
                    None: self._cmd_alert_call_usage,
                },
                'cooldown': self._cmd_alert_cooldown,
                None: self._cmd_alert_usage,
            },
            'remove': {
                'alert': {
                    'call': {
                        'seen': self._cmd_remove_alert_call_seen,
                        'connected': self._cmd_remove_alert_call_connected,
                        None: self._cmd_remove_alert_call_usage,
                    },
                    None: self._cmd_remove_alert_usage,
                },
            },
            'terminate': {
                'bot': self._cmd_terminate_bot,
            },
        }

    async def _dispatch_command(self, fields):
        node = self.command_tree
        i = 0
        while i < len(fields) and isinstance(node, dict):
            token = fields[i]
            if token in node:
                node = node[token]
                i += 1
            else:
                break
        if callable(node):
            await node(fields[i:])
        elif isinstance(node, dict) and None in node:
            await node[None](fields[i:])
        else:
            await self.bot_out_queue.put("Unknown command: type help for help")

    async def _cmd_help(self, fields):
        if fields:
            await self.bot_out_queue.put("Unknown command: type help for help")
            return
        if self.state.fixed_width:
            await self.bot_out_queue.put(BpqInterface.COMMANDS_FIXED)
        else:
            await self.bot_out_queue.put(BpqInterface.COMMANDS)

    async def _cmd_fixed(self, fields):
        usage = "Usage: fixed <on|off>"
        if len(fields) == 1 and fields[0] in ("on", "off"):
            if fields[0] == "on":
                self.state.fixed_width = True
                await self.bot_out_queue.put("Fixed-width font enabled")
            else:
                self.state.fixed_width = False
                await self.bot_out_queue.put("Fixed-width font disabled")
        else:
            await self.bot_out_queue.put(usage)

    async def _cmd_telnet(self, fields):
        if fields:
            await self.bot_out_queue.put("Unknown command: type help for help")
            return
        # telnet_in_queue is only set once telnet_passthru() has connected and authed, so also guard on the
        # task itself so a quick re-issue during connect doesn't start a second session.
        if self.telnet_in_queue is not None or (
                self.telnet_passthru_task is not None and not self.telnet_passthru_task.done()):
            await self.bot_out_queue.put("Already connected to telnet")
            return
        telnet_in_queue = asyncio.Queue()  # Will be set to self.telnet_in_queue once logged in to telnet
        self.telnet_passthru_task = asyncio.create_task(self.telnet_passthru(telnet_in_queue))

    async def _cmd_hash_cmds_telnet(self, fields):
        usage = "Usage: hash_cmds_telnet <on|off>"
        if len(fields) == 1 and fields[0] in ("on", "off"):
            self.hash_cmds_telnet = (fields[0] == "on")
            if self.hash_cmds_telnet:
                await self.bot_out_queue.put("Hash-prefixed bot commands in telnet enabled")
            else:
                await self.bot_out_queue.put("Hash-prefixed bot commands in telnet disabled")
        else:
            await self.bot_out_queue.put(usage)

    async def _cmd_monports(self, fields):
        usage = "Usage: monports <add|del> <portnum>"
        if len(fields) == 0:
            await self.bot_out_queue.put("Monitor set to use ports: "
                                         f"{', '.join(str(port) for port in self.monitor_ports)}\n"
                                         f"{usage}")
            return
        if len(fields) == 2 and fields[0] in ('add', 'del'):
            try:
                port = int(fields[1])
            except (TypeError, ValueError):
                await self.bot_out_queue.put(usage)
                return
            if fields[0] == 'add':
                if port not in self.monitor_ports:
                    self.monitor_ports.append(port)
                    self.set_monitor_ports(self.monitor_ports)
                    if self.fbb_state['monitoring']:
                        await self._fbb_write_safe(f"\\\\\\\\{self.monitor_ports_bin} 1 1 1 0 0 0 1\r".encode('utf-8'))
            else:
                if port in self.monitor_ports:
                    self.monitor_ports.remove(port)
                    self.set_monitor_ports(self.monitor_ports)
                    if self.fbb_state['monitoring']:
                        await self._fbb_write_safe(f"\\\\\\\\{self.monitor_ports_bin} 1 1 1 0 0 0 1\r".encode('utf-8'))
            await self.bot_out_queue.put("Monitor set to use ports: "
                                         f"{', '.join(str(port) for port in self.monitor_ports)}")
            return
        await self.bot_out_queue.put(usage)

    async def _cmd_monfilter(self, fields):
        usage = "Usage: monfilter <add|del> <from|to> <call>"
        if len(fields) == 0:
            await self.bot_out_queue.put(f"Monitor filtering From calls: {self.fmt_monfilter_from()}, "
                                         f"To calls: {self.fmt_monfilter_to()}\n{usage}")
            return
        if len(fields) == 3 and fields[0] in ('add', 'del') and fields[1] in ('from', 'to'):
            call = fields[2].upper()
            if fields[0] == 'add':
                if call not in self.mon_filter[fields[1]]:
                    self.mon_filter[fields[1]].append(call)
            else:
                if call in self.mon_filter[fields[1]]:
                    self.mon_filter[fields[1]].remove(call)
            await self.bot_out_queue.put(f"Monitor filtering From calls: {self.fmt_monfilter_from()}, "
                                         f"To calls: {self.fmt_monfilter_to()}")
            return
        await self.bot_out_queue.put(usage)

    async def _cmd_monitor(self, fields):
        usage = "Usage: monitor <on|off>"
        if len(fields) == 1:
            if fields[0] == 'on':
                if not self.fbb_state['monitoring']:
                    await self.fbb_start_monitor()
                self.fbb_state['bot_monitor'] = True
                await self.bot_out_queue.put(f"Monitor on{self._fbb_connection_status_msg()}")
            elif fields[0] == 'off':
                self.fbb_state['bot_monitor'] = False
                await self.stop_fbb_monitor_if_not_required()
                await self.bot_out_queue.put("Monitor off")
            else:
                await self.bot_out_queue.put(usage)
        else:
            if self.fbb_state['bot_monitor']:
                await self.bot_out_queue.put(f"Monitor is on\n{usage}")
            else:
                await self.bot_out_queue.put(f"Monitor is off\n{usage}")

    async def _cmd_alert_usage(self, fields):
        await self.bot_out_queue.put("Usage: alert <call> [alert_specific_args]")

    async def _cmd_alert_call_usage(self, fields):
        await self.bot_out_queue.put("Usage: alert call <seen|connected> <callsign>")

    async def _cmd_alert_call_seen(self, fields):
        if len(fields) == 1:
            await self.add_alert_call_seen(fields[0])
        else:
            await self._cmd_alert_call_usage(fields)

    async def _cmd_alert_call_connected(self, fields):
        if len(fields) == 1:
            await self.add_alert_call_connected(fields[0])
        else:
            await self._cmd_alert_call_usage(fields)

    async def _cmd_alert_cooldown(self, fields):
        usage = f"Usage: alert cooldown <seconds>  (0 disables; current: {self.alert_cooldown})"
        if len(fields) == 0:
            await self.bot_out_queue.put(usage)
            return
        if len(fields) == 1:
            try:
                secs = int(fields[0])
                if secs < 0:
                    raise ValueError
            except ValueError:
                await self.bot_out_queue.put(usage)
                return
            self.alert_cooldown = secs
            self._alert_last_fired.clear()
            if secs == 0:
                await self.bot_out_queue.put("Alert cooldown disabled")
            else:
                await self.bot_out_queue.put(f"Alert cooldown set to {secs}s")
            return
        await self.bot_out_queue.put(usage)

    async def _cmd_remove_alert_usage(self, fields):
        await self.bot_out_queue.put("Usage: remove alert <call> [alert_specific_args]")

    async def _cmd_remove_alert_call_usage(self, fields):
        await self.bot_out_queue.put("Usage: remove alert call <seen|connected> <callsign>")

    async def _cmd_remove_alert_call_seen(self, fields):
        if len(fields) == 1:
            await self.remove_alert_call_seen(fields[0])
        else:
            await self._cmd_remove_alert_call_usage(fields)

    async def _cmd_remove_alert_call_connected(self, fields):
        if len(fields) == 1:
            await self.remove_alert_call_connected(fields[0])
        else:
            await self._cmd_remove_alert_call_usage(fields)

    async def _cmd_terminate_bot(self, fields):
        if fields:
            await self.bot_out_queue.put("Unknown command: type help for help")
            return
        if self.telnet_in_queue is not None:
            await self.bot_out_queue.put("Terminate Bot - Are you sure? You will not be able to interact with "
                                         "the bot until you restart it on the node. Reply '#yes' to confirm, "
                                         "or '#no' to abort.")
        else:
            await self.bot_out_queue.put("Terminate Bot - Are you sure? You will not be able to interact with "
                                         "the bot until you restart it on the node. Reply 'yes' to confirm.")
        self.command_state = 'terminate_bot_confirm'

    async def handle_message_tenet_passthru(self, message):
        # Once we've detected a '#' prefix, treat it as bot input and lowercase for comparison/dispatch
        # (matches the convention used by process_bot_incoming for non-telnet input). Plain telnet input
        # keeps its original case so case-sensitive payloads (e.g. BBS message text) pass through clean.
        if message.startswith('#'):
            lowered = message.lower()
            if self.command_state == 'terminate_bot_confirm':
                # Mirror non-telnet confirmation semantics within the '#' space: '#yes' confirms, any
                # other '#'-prefixed reply aborts and is consumed. Non-'#' telnet input falls through.
                if lowered == '#yes':
                    await self.bot_out_queue.put("Bot Terminating - bye!")
                    self.terminated.set()
                    return
                self.command_state = None
                await self.bot_out_queue.put("Bot Terminate aborted")
                return
            if lowered == '#quit':
                self.telnet_passthru_task.cancel()
                return
            if self.hash_cmds_telnet and len(lowered) > 1:
                await self._dispatch_command(lowered[1:].split())
                return
        await self.telnet_in_queue.put(message)

    async def keepalive_nulls(self, writer, interval_secs=540):
        while not self.terminated.is_set():
            await asyncio.sleep(interval_secs)
            if writer.is_closing():
                print("keepalive_nulls(): writer is closing, exiting")
                return
            try:
                writer.write(b"\x00")
                await writer.drain()
            except Exception as e:
                print(f"keepalive_nulls(): write failed, exiting: {e}")
                return

    async def fbb_start_monitor(self):
        self.fbb_state['monitoring'] = True
        if self.fbb_connection_task is None:
            self.fbb_connection_task = asyncio.create_task(self.fbb_connection())
        # If already connected, push the current port mask now. If not, the task sends it itself once it
        # (re)connects, so this is a no-op.
        await self._fbb_write_safe(f"\\\\\\\\{self.monitor_ports_bin} 1 1 1 0 0 0 1\r".encode('utf-8'))

    def _fbb_connection_status_msg(self):
        if self.fbb_state['monitoring'] and self.fbb_down_notified:
            return " (FBB currently disconnected - will activate when reconnected)"
        return ""

    async def _fbb_open_and_auth(self):
        if ('fbb_host' not in self.conf['bpq'] or 'fbb_port' not in self.conf['bpq'] or
           'fbb_user' not in self.conf['bpq'] or 'fbb_pass' not in self.conf['bpq']):
            raise FbbAuthError("Missing fbb config options under 'bpq', expected: fbb_host, fbb_port, "
                               "fbb_user, fbb_pass")
        try:
            self.fbb_reader, self.fbb_writer = await asyncio.open_connection(
                self.conf['bpq']['fbb_host'], self.conf['bpq']['fbb_port'])

            sock = self.fbb_writer.get_extra_info('socket')
            if sock is not None:
                try:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                except OSError as e:
                    print(f"Could not enable SO_KEEPALIVE on FBB socket: {e}")

            self.fbb_keepalive_task = asyncio.create_task(self.keepalive_nulls(self.fbb_writer))

            self.fbb_writer.write(f"{self.conf['bpq']['fbb_user']}\r{self.conf['bpq']['fbb_pass']}\r"
                                  "BPQTERMTCP\r\\\\\\\\0 0 0 0 0 0 0 0\r".encode('utf-8'))
            await self.fbb_writer.drain()

            # We sent a no-monitor setting above, so we expect either a connected message ending in "\r" or
            # a "password:" prompt if creds were rejected. (async_read swallows its own timeout and returns
            # whatever it had buffered, so a silent timeout just looks like an empty/partial response — the
            # read loop will then time out and trigger a reconnect.)
            message = await self.async_read(self.fbb_reader, timeout=5, decode=True, separator=b'\r')
            if message == 'password:':
                raise FbbAuthError("FBB user/pass not accepted")
        except Exception as e:
            print(f"ERROR in _fbb_open_and_auth(): {e}")
            self._teardown_fbb_socket()
            raise

    def _teardown_fbb_socket(self):
        if self.fbb_keepalive_task is not None:
            self.fbb_keepalive_task.cancel()
            self.fbb_keepalive_task = None
        if self.fbb_writer is not None:
            try:
                self.fbb_writer.close()
            except Exception as e:
                print(f"Error closing FBB writer: {e}")
            self.fbb_writer = None
        self.fbb_reader = None

    async def _sleep_or_terminate(self, delay):
        try:
            await asyncio.wait_for(self.terminated.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass

    async def _fbb_write_safe(self, data):
        if self.fbb_writer is None:
            print(f"FBB write skipped (not connected): {data!r}")
            return
        self.fbb_writer.write(data)
        await self.fbb_writer.drain()

    async def fbb_connection(self):
        # Owns the full FBB connection lifecycle: initial connect, monitor enable, read loop, and any
        # reconnects after a drop. All user-facing FBB connection-state messages are emitted from here so
        # that the user sees exactly one message per state transition (down / back up), regardless of how
        # many retry attempts happen in between.
        delay = self.fbb_reconnect_initial_delay
        was_ever_connected = False
        try:
            while not self.terminated.is_set() and self.fbb_state['monitoring']:
                try:
                    await self._fbb_open_and_auth()
                    # Enable monitor with the current port mask. Folded into the same try block so a
                    # failure here (e.g. peer hung up immediately after auth) is handled the same way as
                    # any other connect failure.
                    self.fbb_writer.write(f"\\\\\\\\{self.monitor_ports_bin} 1 1 1 0 0 0 1\r".encode('utf-8'))
                    await self.fbb_writer.drain()
                except FbbAuthError as e:
                    # Not transient — no point retrying. Tell the user and stop.
                    print(f"FBB auth/config error, stopping reconnect: {e}")
                    await self.bot_out_queue.put(f"FBB error: {e} - monitor/alerts disabled")
                    self.fbb_state['monitoring'] = False
                    break
                except Exception as e:
                    print(f"FBB connect failed: {e}")
                    self._teardown_fbb_socket()
                    if not self.fbb_down_notified:
                        if was_ever_connected:
                            await self.bot_out_queue.put("Lost FBB connection - will keep retrying in background")
                        else:
                            await self.bot_out_queue.put("Could not connect to FBB - will keep retrying in background")
                        self.fbb_down_notified = True
                    await self._sleep_or_terminate(delay)
                    delay = min(delay * 2, self.fbb_reconnect_max_delay)
                    continue

                if self.fbb_down_notified:
                    if was_ever_connected:
                        await self.bot_out_queue.put("FBB reconnected, alerts active again")
                    else:
                        await self.bot_out_queue.put("FBB connected, monitor/alerts active")
                    self.fbb_down_notified = False
                was_ever_connected = True
                delay = self.fbb_reconnect_initial_delay

                try:
                    await self._fbb_read_loop()
                except Exception as e:
                    print(f"Error in fbb read loop: {e}")
                # Read loop returned/raised -> connection dead.
                self._teardown_fbb_socket()
                if self.terminated.is_set() or not self.fbb_state['monitoring']:
                    break
                # Notify of the drop now (rather than waiting for the first failed reconnect attempt) so
                # an immediate successful reconnect still produces the expected down/up message pair.
                if not self.fbb_down_notified:
                    await self.bot_out_queue.put("Lost FBB connection - will keep retrying in background")
                    self.fbb_down_notified = True
        finally:
            self._teardown_fbb_socket()
            self.fbb_connection_task = None

    async def _fbb_read(self, n):
        return await asyncio.wait_for(self.fbb_reader.read(n), timeout=self.fbb_read_idle_timeout)

    async def _fbb_readuntil(self, separator):
        return await asyncio.wait_for(self.fbb_reader.readuntil(separator), timeout=self.fbb_read_idle_timeout)

    async def _fbb_read_loop(self):
        while not self.terminated.is_set():
            try:
                byte = await self._fbb_read(1)
                if len(byte) == 0:
                    return  # EOF, ie. remote end disconnected
                elif byte[0] == 0xff:
                    # Monitor output, see if it is a portmap or an actual monitored packet
                    byte = await self._fbb_read(1)
                    while(len(byte) == 0):
                        byte = await self._fbb_read(1)
                    if byte[0] == 0xff:
                        message = await self._fbb_readuntil(b'|')
                        message = message[:-1]  # Remove the trailing '|'
                        try:
                            port_count = int(message)
                            ports = []
                            for i in range(0, port_count):
                                message = await self._fbb_readuntil(b'|')
                                ports.append(message[:-1])
                            print(f"FBB monitor portmap received: port count: {port_count}, ports: {ports}")
                        except Exception as e:
                            print("FBB error parsing portmap: {e}")
                    elif byte[0] == 0x1b:
                        byte = await self._fbb_read(1)
                        while(len(byte) == 0):
                            byte = await self._fbb_read(1)
                        if byte[0] == 0x11 or byte[0] == 0x5b:  # These are terminal colour codes
                            message = await self._fbb_readuntil(b'\xfe')
                            message = message[:-1]  # Remove the trailing \xfe
                            if self.fbb_state['bot_monitor']:
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
                    message = await self._fbb_readuntil(b'\r')
                    message = byte + message[:-1]  # Add on the first byte received originally, and remove trailing \r
                    print(f"FBB non-monitor received: {message}")
            except asyncio.TimeoutError:
                print(f"FBB read idle timeout ({self.fbb_read_idle_timeout}s) — treating connection as dead")
                return

    # This looks a lot like asyncio.readuntil() or asyncio.readline() but we implement it here so we can get a partial
    # read buffer even if no separator was seen after a certain timeout, see: async_read(). Additionally, it allows
    # reading a larger buffer in one go even if it includes several separators, which can be more efficient and makes it
    # easier if forwarding to the bot to send one big chunk rather than lots of individual chunks/lines that can be rate
    # limited.
    async def asyncio_readuntil_or_partial(self, reader, buffer, separator=b'\n'):
        got_newline = False
        while not got_newline:
            chunk = await reader.read(10000)
            if len(chunk) == 0:
                # EOF — peer closed. Surface whatever was buffered so far via ConnectionResetError.
                raise ConnectionResetError("peer closed connection")
            buffer.write(chunk)
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

    async def telnet_passthru(self, telnet_in_queue):
        if ('telnet_host' not in self.conf['bpq'] or 'telnet_port' not in self.conf['bpq'] or
           'telnet_user' not in self.conf['bpq'] or 'telnet_pass' not in self.conf['bpq']):
            await self.bot_out_queue.put("Missing telnet config options under 'bpq;, expected: telnet_host, "
                                         "telnet_port, telnet_user, telnet_pass")
            return
        keepalive = None
        telnet_reader = telnet_writer = None
        outgoing_task = None
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

            # Sign on as a BPQTERMTCP terminal client. Send user, password and the
            # BPQTERMTCP magic in a single write with no prompt waits between lines --
            # this matches QtTermTCP.cpp ~line 4938 ("%s\r%s\rBPQTERMTCP\r") and is what
            # the configured BPQ port must be expecting for this to work.
            #
            # The 'telnet_port' in our config must therefore point at a BPQ telnet listener
            # configured as a BPQTERMTCP application (in practice: the same port BPQ uses
            # for FBB / what fbb_port points at). Pointing it at a plain Telnet application
            # port will fail: BPQ would present interactive user:/password: prompts and
            # then drop us at the node command line, where 'BPQTERMTCP' is just an
            # unrecognised command. More importantly for our use case, in plain telnet
            # mode BPQ's telnet layer eats incoming NUL bytes (per RFC 854 NUL is a no-op)
            # so keepalive_nulls() keeps the TCP socket alive but never reaches the node
            # session -- after ~15 min BPQ emits "Disconnected from Stream N / Disconnected
            # from Node - Telnet Session kept" and the node session is lost while the
            # socket survives. In BPQTERMTCP mode BPQ bypasses telnet protocol
            # interpretation, so the NULs reach the session layer and act as a real
            # keepalive (this is also why the FBB monitor connection, which already sends
            # BPQTERMTCP in _fbb_open_and_auth(), doesn't suffer the same drop).
            telnet_writer.write(f"{self.conf['bpq']['telnet_user']}\r"
                                f"{self.conf['bpq']['telnet_pass']}\r"
                                "BPQTERMTCP\r".encode('utf-8'))
            await telnet_writer.drain()

            # Now we are connected and logged in, we can start accepting telnet input via the bot, setting
            # self.telnet_in_queue will cause process_bot_incoming() to make use of it
            self.telnet_in_queue = telnet_in_queue
            outgoing_task = asyncio.create_task(self.telnet_passthru_outgoing(telnet_writer))

            while not self.terminated.is_set():
                try:
                    # BPQTERMTCP terminates lines with \r only (not \r\n like plain
                    # telnet), so flush on \r — otherwise the connect banner sits in
                    # the buffer until async_read's fallback timeout, or until BPQ
                    # later emits a \n, whichever comes first.
                    message = await self.async_read(telnet_reader, separator=b'\r')
                    if len(message.rstrip()) > 0:
                        #print(f"telnet received: {message}")
                        await self.bot_out_queue.put(message)
                except asyncio.CancelledError:
                    raise
                except ConnectionResetError:
                    print("Telnet connection closed by remote node")
                    await self.bot_out_queue.put("Telnet connection closed by remote node")
                    break
                except Exception as e:
                    print(f"Error in telnet_passthru() during async_read loop: {e}")
                    try:
                        await self.bot_out_queue.put(message)
                    except:
                        pass
        except asyncio.CancelledError:
            if telnet_writer is not None:
                telnet_writer.write("b\r".encode('utf-8'))
                try:
                    await asyncio.wait_for(telnet_writer.drain(), timeout=5)
                    # Last chance read for whatever is sent as we're quiting telnet
                    message = await self.async_read(telnet_reader, separator=b'\r')
                    if len(message.rstrip()) > 0:
                        await self.bot_out_queue.put(message)
                except (asyncio.exceptions.TimeoutError, asyncio.LimitOverrunError,
                        asyncio.IncompleteReadError, ConnectionResetError, BrokenPipeError) as e:
                    pass  # Oh well, we're quitting
            raise  # asyncio.CancelledError expects to be propogated after cleanup
        finally:
            if outgoing_task is not None:
                outgoing_task.cancel()
                await asyncio.gather(outgoing_task, return_exceptions=True)
            if keepalive is not None:
                keepalive.cancel()
            if telnet_writer is not None:
                telnet_writer.close()
            await self.bot_out_queue.put("Telnet passthru terminated")
            self.telnet_in_queue = None

    async def telnet_passthru_outgoing(self, telnet_writer):
        while not self.terminated.is_set():
            try:
                message = await asyncio.wait_for(self.telnet_in_queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            try:
                telnet_writer.write(f"{message}\r".encode('utf-8'))
                await telnet_writer.drain()
                #print(f"Telnet sent: {message}")
            except asyncio.CancelledError:
                raise
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
        state = packetnodebot.common.BotState(fixed_width=bool(conf.get('fixed_width_font', False)))
        bpq = BpqInterface(conf, bot_in_queue, bot_out_queue, terminated, state)

        if conf['bot_connector'] == 'discord':
            intents = discord.Intents.default()
            intents.message_content = True
            intents.members = True
            bot_connector = packetnodebot.discord.DiscordConnector(conf=conf, conf_file='packetnodebot.yaml',
                                                                   terminated=terminated, bot_in_queue=bot_in_queue,
                                                                   bot_out_queue=bot_out_queue, state=state,
                                                                   intents=intents)
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
