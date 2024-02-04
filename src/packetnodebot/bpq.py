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

    # TODO handle disconnect, and we need a Queue to SEND commands over telnet too
    async def telnet_passthru(self):
        self.telnet_reader, self.telnet_writer = await asyncio.open_connection('127.0.0.1', 8010)
        print("about to login")

        await asyncio.sleep(2) ### TODO THIS IS A HACK!

        r = await self.telnet_reader.read(1000)
        print(f"recvd: {r}")

        await asyncio.sleep(2) ### TODO THIS IS A HACK!

        self.telnet_writer.write("2E0HKD\r\n".encode('utf-8'))
        print("callsign sent")
        
        await asyncio.sleep(2) ### TODO THIS IS A HACK!

        r = await self.telnet_reader.read(1000)
        print(f"recvd: {r}")

        await asyncio.sleep(2) ### TODO THIS IS A HACK!
        
        self.telnet_writer.write("R@dio\r\n".encode('utf-8'))
        print("pass sent")
        
        await asyncio.sleep(2) ### TODO THIS IS A HACK!

        while True:
            msg_bytes = await self.telnet_reader.readline()
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
