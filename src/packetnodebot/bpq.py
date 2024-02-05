import asyncio
import yaml
import discord
import packetnodebot.discord


class BpqInterface():
    def __init__(self, bot_in_queue, bot_out_queue):
        self.bot_in_queue = bot_in_queue
        self.bot_out_queue = bot_out_queue

    async def process_bot_incoming(self):
        try:
            while True:  ## TODO have some flag we set when we quit or whatever
                message = await self.bot_in_queue.get()
                print(f"BpqInterface received: {message}")

                if message == 'hello':
                    await self.bot_out_queue.put("Hey there")

                self.bot_in_queue.task_done()
        except Exception as e:
            print(e)


    ### NOTE:
    ### telnet server implements telnet control protocol, FBB port does not.
    ### would be nice to support both?
    ### FBB does not send prompts for user and pass, so no need to read back before sending them... and it seems to send BPQTERMTCP afterwards to go into whatever mode that uses
    ###
    ### SO: when we config whether to use telnet or FBB, react differently accordingly - may do FBB first, because we'll use that anyway for monitoring, and no need to do telnet control protocol!
    ###
    ### There is an asyncio telnet client, but it is not necessarily well maintained and may have bugs, so I'd like to just handle it directly on an MVP basis


    # TODO handle disconnect, and we need a Queue to SEND commands over telnet too
    async def telnet_passthru(self):
        self.telnet_reader, self.telnet_writer = await asyncio.open_connection('127.0.0.1', 8010)
        print("about to login")

        # Wait for user prompt
        try:
            await asyncio.wait_for(self.telnet_reader.readuntil(b':'), timeout=5)
        except TimeoutError:
            print("The long operation timed out, but we've handled it.")  ## TODO probably bail, allow reconnect later or whatever - maybe send error to bot?
        except (asyncio.LimitOverrunError, asyncio.IncompleteReadError) as e:
            print(f"asyncio error waiting for user prompt: {e}")  ## TODO probably bail, allow reconnect later or whatever - maybe send error to bot?

        self.telnet_writer.write("2E0HKD\r".encode('utf-8'))
        await self.telnet_writer.drain()
        print("callsign sent")

         # Wait for pass prompt
        try:
            await asyncio.wait_for(self.telnet_reader.readuntil(b':'), timeout=5)
        except TimeoutError:
            print("The long operation timed out, but we've handled it.")  ## TODO probably bail, allow reconnect later or whatever - maybe send error to bot?
        except (asyncio.LimitOverrunError, asyncio.IncompleteReadError) as e:
            print(f"asyncio error waiting for user prompt: {e}")  ## TODO probably bail, allow reconnect later or whatever - maybe send error to bot?

        self.telnet_writer.write("R@dio\r".encode('utf-8'))
        await self.telnet_writer.drain()
        print("pass sent")
        
        #self.telnet_writer.write("BPQTERMTCP\r".encode('utf-8'))
        #await self.telnet_writer.drain()
        #print("BPQTERMTCP sent")

        #self.telnet_writer.write("\\\\100000003 1 1 1 1 0 0 1\r".encode('utf-8'))
        #await self.telnet_writer.drain()
        #print("\\\\100000003 1 1 1 1 0 0 1 sent")

        #self.telnet_writer.write("?\r".encode('utf-8'))
        #await self.telnet_writer.drain()
        #print("? sent")

        while True:
            msg_bytes = await self.telnet_reader.readline()   ### TODO try to make this a coroutine that then yields here instead?
            if not msg_bytes:
                print("telnet connection lost")  ##### atually does this just mean "no more to read for now"?
                break
            msg_str = msg_bytes.decode("utf-8")
            print(f"telnet received: {msg_str}")  ## TODO we get blank lines sometimes, these cannot be sent over discord! pre-pending "telnet: " fixes it for now, but we need to do better..
            await self.bot_out_queue.put("telnet: " + msg_str)


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

        telnet_passthru_task = asyncio.create_task(bpq.telnet_passthru())

        await asyncio.gather(discord_task, bpq_process_bot_in_task, discord_process_bot_out_task, telnet_passthru_task)

    except Exception as e:
        print(e)

def bpqnodebot():
    try:
        asyncio.run(main())
    except Exception as e:
        print(e)
