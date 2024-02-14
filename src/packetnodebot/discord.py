import yaml
import discord


class DiscordConnector(discord.Client):
    def __init__(self, *args, conf, conf_file, bot_in_queue, bot_out_queue, **kwargs,):
        super().__init__(*args, **kwargs)
        self.conf = conf
        self.conf_file = conf_file
        self.bot_in_queue = bot_in_queue
        self.bot_out_queue = bot_out_queue
        self.authed_member = None

    async def on_ready(self):
        print(f'Connected to discord as {self.user} (ID: {self.user.id})')
        if 'sysop_user_id' in self.conf['discord']:
            self.authed_member = await self.fetch_user(self.conf['discord']['sysop_user_id'])
            print(f"Authorised discord user: {self.authed_member}")

    async def on_message(self, message):
        try:
            if self.authed_member is None and message.content.startswith('register'):
                usage_register = "Usage: register <register_sysop_user_id_bot_password>"
                fields = message.content.split(' ')
                if len(fields) == 2:
                    if self.conf['discord']['register_sysop_user_id_bot_password'] == fields[1]:
                        self.authed_member = message.author
                        try:
                            self.conf['discord']['sysop_user_id'] = self.authed_member.id
                            with open(self.conf_file, 'w') as file:
                                yaml.dump(self.conf, file)
                            await self.bot_out_queue.put("You are now the registered user")
                        except Exception as e:
                            await self.bot_out_queue.put(f"You are now the registered user - however, this cannot be "
                                                         "persisted over a restart as there was an error writing your "
                                                         "user ID to the config file: {e}")
                    else:
                        await message.author.send("Incorrect password")
                else:
                    await message.author.send(usage_register)
            elif self.authed_member is not None and self.authed_member.id == message.author.id:
                await self.bot_in_queue.put(message.content)
        except Exception as e:
            print(e)

    async def process_bot_outgoing(self):
        try:
            while True:
                message = await self.bot_out_queue.get()
                if self.authed_member is not None:
                    await self.authed_member.send(message)
                    print(f"DiscordConnector sent: {message}")
                else:
                    print(f"Not sending message as no registered user populated yet")
                self.bot_out_queue.task_done()
        except Exception as e:
            print(e)
