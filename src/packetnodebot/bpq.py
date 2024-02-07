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

                ## TODO DEBUG this just lets us check the bot is alive while we're testing
                if message == 'hello':
                    await self.bot_out_queue.put("Hey there")

                self.bot_in_queue.task_done()
        except Exception as e:
            print(e)
    
    async def handle_message_tenet_passthru(self, message):
        if message == '#quit':
            self.telnet_passthru_task.cancel()
        else:
            await self.telnet_in_queue.put(message)


    ### NOTE:
    ### telnet server implements telnet control protocol, FBB port does not.
    ### would be nice to support both?
    ### FBB does not send prompts for user and pass, so no need to read back before sending them... and it seems to send BPQTERMTCP afterwards to go into whatever mode that uses
    ###
    ### SO: when we config whether to use telnet or FBB, react differently accordingly - may do FBB first, because we'll use that anyway for monitoring, and no need to do telnet control protocol!
    ###
    ### There is an asyncio telnet client, but it is not necessarily well maintained and may have bugs, so I'd like to just handle it directly on an MVP basis

    async def fbb_connection(self, ):

        ### TODO this will connect to FBB, handle login and receiving of the port map
        ### We'll need a KEEPALIVE though, that shoul dbe atimer type task that runs separately and submits a keepalive to the Queue
        ###     TELNET PASSTHRU ALSO NEEDS THIS BTW! (both just send a \x00)
        ###
        ### FBB also probably needs a protocol state machine of sorts to know when to finish reading - like the portmap ends with "|" not a newline.
        ###
        ### BUT: when we MONITOR we'll need to ALSO read freely, as monitor frames can come in any time, so maybe this DOES need a separate read task and Queue more like other things after all....
        ### So this task just needs to be connect, and send Queue input to FBB - a separate task will READ from FBB (NOT THIS ONE!) and push to queue (so more like the bot read, all aync, not the telnet passthru which does not yet work that way)

        pass



    # This looks a lot like asyncio.readline() but we implement it here so we can customise it such that we can get a
    # partial read buffer even if no newline was seen after a certain timeout, see: telnet_read()
    async def telnet_readline(self, buffer):
        got_newline = False
        while not got_newline:
            bytes = await self.telnet_reader.read(10000)
            buffer.write(bytes)
            if buffer.getvalue().endswith(b"\n"):
                got_newline = True

    # reads until a newline, or there is a timeout, in which case return whatever was already read into the buffer.
    # This means we will not garble most messages that do end in a newline, but if there is one that does not (like a
    # prompt) it will still get sent back to the user.
    async def telnet_read(self, timeout=5):
        buffer = BytesIO()
        try:
            await asyncio.wait_for(self.telnet_readline(buffer), timeout=timeout)
        except asyncio.exceptions.TimeoutError:
            pass  # If we timeout, just send whatever we have in the buffer even though it does not end in a newline
        message = buffer.getvalue().decode('utf-8', 'ignore')
        return message


    #### TODO CONVERT telnet passthru to ALSO use an ASYNC READ (that maybe uses a read Queue) more like the bot - if the user enters LISTEN mode or CHAT, we need to be able to asynchronously read input as it comes, not only after a user command!

    # TODO handle disconnect, and we need a Queue to SEND commands over telnet too
    async def telnet_passthru(self, telnet_in_queue):
        try:
            await self.bot_out_queue.put("Entering telnet passthru mode, all further messages will be sent directly to a logged in telnet session. To exit telnet passthru send: #quit")

            try:
                self.telnet_reader, self.telnet_writer = await asyncio.open_connection('127.0.0.1', 8010)
            except ConnectionRefusedError:
                await self.bot_out_queue.put("Could not connect to telnet - exiting telnet passthru mode")
                return

            # Wait for user prompt
            try:
                await asyncio.wait_for(self.telnet_reader.readuntil(b':'), timeout=5)
            except asyncio.exceptions.TimeoutError:
                print("The long operation timed out, but we've handled it.")  ## TODO probably bail, allow reconnect later or whatever - maybe send error to bot?
            except (asyncio.LimitOverrunError, asyncio.IncompleteReadError) as e:
                print(f"asyncio error waiting for user prompt: {e}")  ## TODO probably bail, allow reconnect later or whatever - maybe send error to bot?

            self.telnet_writer.write("2E0HKD\r".encode('utf-8'))
            await self.telnet_writer.drain()

            # Wait for pass prompt
            try:
                await asyncio.wait_for(self.telnet_reader.readuntil(b':'), timeout=5)
            except asyncio.exceptions.TimeoutError:
                print("The long operation timed out, but we've handled it.")  ## TODO probably bail, allow reconnect later or whatever - maybe send error to bot?
            except (asyncio.LimitOverrunError, asyncio.IncompleteReadError) as e:
                print(f"asyncio error waiting for user prompt: {e}")  ## TODO probably bail, allow reconnect later or whatever - maybe send error to bot?

            ### TODO add a timeout to writes as well? Maybe wrap into a method
            self.telnet_writer.write("R@dio\r".encode('utf-8'))
            await self.telnet_writer.drain()

            # Now we are connected and logged in, we can start accepting telnet input via the bot, setting
            # self.telnet_in_queue will cause process_bot_incoming() to make use of it
            self.telnet_in_queue = telnet_in_queue
            asyncio.create_task(self.telnet_passthru_outgoing())

            while True:
                message = await self.telnet_read()
                if len(message.rstrip()) > 0:
                    print(f"telnet received: {message}")
                    await self.bot_out_queue.put(message)
        except asyncio.CancelledError:
            self.telnet_writer.write("b\r".encode('utf-8'))
            try:
                await asyncio.wait_for(await self.telnet_writer.drain(), timeout=5)
                # Last chance read for whatever is sent as we're quiting telnet
                message = await self.telnet_read()
                if len(message.rstrip()) > 0:
                    await self.bot_out_queue.put(message)
            except (asyncio.exceptions.TimeoutError, asyncio.LimitOverrunError, asyncio.IncompleteReadError) as e:
                pass  # Oh well, we're quitting 
        finally:
            await self.bot_out_queue.put("Telnet passthru terminated")
            self.telnet_in_queue = None  ## TODO also ideally do this if the remote end closes the telnet connection, and in that case also send a message via the bot to let the user know what happened

    async def telnet_passthru_outgoing(self):
        try:
            while True:  ## TODO have some flag we set when we quit or whatever
                message = await self.telnet_in_queue.get()
                self.telnet_writer.write(f"{message}\r".encode('utf-8'))
                await self.telnet_writer.drain()
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

        await asyncio.gather(discord_task, bpq_process_bot_in_task, discord_process_bot_out_task)

    except Exception as e:
        print(e)

def bpqnodebot():
    try:
        asyncio.run(main())
    except Exception as e:
        print(e)
