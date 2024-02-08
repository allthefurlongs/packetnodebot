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
        }
        self.fbb_connection_task = None
        self.fbb_writer = None
        self.fbb_reader = None

    async def process_bot_incoming(self):
        try:
            while True:  ## TODO have some flag we set when we quit or whatever
                message = await self.bot_in_queue.get()
                message = message.rstrip()
                print(f"BpqInterface received: {message}")

                if self.telnet_in_queue is not None:
                    await self.handle_message_tenet_passthru(message)
                elif message == 'telnet passthru':
                    telnet_in_queue = asyncio.Queue()  # Will be set to self.telnet_in_queue once logged in to telnet
                    self.telnet_passthru_task = asyncio.create_task(self.telnet_passthru(telnet_in_queue))
                elif message.startswith('alert call seen'):
                    fields = message.split(' ')
                    if len(fields) >= 4:
                        callsign = fields[3]
                        if not self.fbb_state['monitoring']:
                            await self.fbb_start_monitor()

                        # callsign ...


                ## TODO HACK just to test some stuff
                elif message == 'fbb':
                    await self.ensure_fbb_connected()


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
        self.fbb_writer.write(b"\\\\\\\\3 1 1 1 1 0 0 1\r")  ## TODO hardcoded portmap here enabling the first few ports, get from config or whatever later
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

                ## We expect a message ending in a newline confirming connection to telnet
                #message = await self.async_read(self.fbb_reader, decode=False)
                #print(f"FBB received: {message}")

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


    async def fbb_connection(self):

        ### TODO this will connect to FBB, handle login and receiving of the port map
        ###
        ### FBB also probably needs a protocol state machine of sorts to know when to finish reading - like the portmap ends with "|" not a newline.

        try:
            while True:
                ### TODO QUESTION: do we want to use async_read() here which was designed to give telnet passthru as much data in one go as possible
                ### or, do we want to have more of a asyncio.readuntil() approach if EVERYthing we expect after connecting will end with a newline?
                ### ... and actually readline() will do right? we care less about timeout here - IF we expect a newline, we can wait for a newline....
                ###      if we change what we monitor we'll get a new port map ending in '|', but we can handle that separately... hell we can just reconnect worst case...
                ###
                ### TODO TODO THE BELOW IN FACT!
                ### OH NO!!! It does NOT all end in a newline... monitor output ends in \xfe -- ok so how about this: MODIFY async_read() to accept an ARRAY of separators.
                ### OR BETTER: do make a new method, DO NOT try to get as much data all in one message as possible (thats for bot passthru's!), and instead:
                ###    Have an array of separators
                ###    When we get a message ending in one of them SPLIT UP what we have into however many messages we have
                ###    Try to yield each message individually
                ###        THIS MEANS: no shared buffer, just some internal buffer and we keep track of what we ave and split it into messages based on all the separators and yirld them one by one.... YEAH!
                ###
                ### If it does, then yes, it will simplify things.
                ### I think everything except port list if we were to change what we monitor will be like this.. but will need to experiment
                message = await self.async_read(self.fbb_reader, decode=False)
                #message = await self.fbb_reader.readline()
                if message == b'Connected to TelnetServer\r':
                    print("FBB Connected-to-telnet received")  # pass
                if message.startswith(b'\xff\x1b\x11'):  ## WILL ACTUALLY end with \xfe when we get message chopping done as in above comment
                    print(f"FBB MONITOR Portmap received: {message}")
                elif message.startswith(b'\xff\xff'):  ## WILL ACTUALLY end with b'|' when we get message chopping done
                    print(f"FBB MONITOR received: {message}")
                else:
                    ### TODO DEBUG CATCH ALL
                    print(f"FBB received: {message}")
                    if len(message.rstrip()) > 0:
                        await self.bot_out_queue.put(message) ### TODO USUALLY WE WILL NOT SEND TO THE BOT, but using it for debugging

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
            while True:  ## TODO have some flag we set when we quit or whatever
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
