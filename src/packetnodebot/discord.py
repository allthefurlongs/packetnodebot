import discord

### TODO maybe make this a separate pip package entirely, so you can: pip install packetnodebot-discord
### That way we do not need to force all the deps for every bot connector to be installed, just the ones you want to use

class DiscordConnector(discord.Client):
    def __init__(self, *args, bot_in_queue, bot_out_queue, **kwargs,):
        super().__init__(*args, **kwargs)
        self.bot_in_queue = bot_in_queue
        self.bot_out_queue = bot_out_queue
        self.sent_from = None
    
    async def on_ready(self):
        print(f'Logged in as {self.user} (ID: {self.user.id})')
        print('------')

    async def on_message(self, message):
        try:
            # we do not want the bot to reply to itself
            if message.author.id == self.user.id:
                return

            ## TODO eventually we'll need to pull out some other basics like user id or whatever in a generic way for abstraction and also auth, for now just send the message content itself
            #await self.node_interface.received(message.content)
            await self.bot_in_queue.put(message.content)

            self.sent_from = message.author  ## TODO this just saves the author of the last message - obviously we'll need a way to manage who we're talking to better than this eventually! Probably bot commands with registration password or something and save something somewhere

            # TODO when we have saved a user id that we want to interact with in future, we can re-get the "member" instance in order to send them messages using the user id which we will save, like this:
            # member = self.get_user(self.sent_from.id)
            # await member.send("hey again!")

        except Exception as e:
            print(e)
    
    async def process_bot_outgoing(self):
        try:
            while True:  ## TODO have some flag we set when we quit or whatever
                message = await self.bot_out_queue.get()
                if self.sent_from is not None:
                    await self.sent_from.send(message)
                    print(f"DiscordConnector sent: {message}")
                else:
                    print(f"Not sending message as no member populated yet")
                self.bot_out_queue.task_done()
        except Exception as e:
            print(e)
