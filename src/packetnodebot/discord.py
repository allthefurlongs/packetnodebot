import yaml
import discord
import packetnodebot.common


class DiscordConnector(discord.Client):
    def __init__(self, *args, conf, conf_file, terminated, bot_in_queue, bot_out_queue, **kwargs,):
        super().__init__(*args, **kwargs)
        self.conf = conf
        self.conf_file = conf_file
        self.terminated = terminated
        self.bot_in_queue = bot_in_queue
        self.bot_out_queue = bot_out_queue
        self.authed_member = None
        if 'fixed_width_font' in conf and conf['fixed_width_font']:
            self.fixed_width = True
        else:
            self.fixed_width = False

    async def on_ready(self):
        print(f'Connected to discord as {self.user} (ID: {self.user.id})')
        if 'sysop_user_id' in self.conf['discord']:
            self.authed_member = await self.fetch_user(self.conf['discord']['sysop_user_id'])
            print(f"Authorised discord user: {self.authed_member}")

    async def send(self, message, member=None):
        if member is None:
            member = self.authed_member
        if self.fixed_width:
            await member.send(f"```{message}```")
        else:
            await member.send(message)

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
                        await self.send("Incorrect password", member=message.author)
                else:
                    await self.send(usage_register, member=message.author)
            elif self.authed_member is not None and self.authed_member.id == message.author.id:
                await self.bot_in_queue.put(message.content)
        except Exception as e:
            print(f"DiscordConnector error in on_message(): {e}")

    async def process_bot_outgoing(self):
        try:
            while not self.terminated.is_set():
                message = await self.bot_out_queue.get()
                if type(message) is packetnodebot.common.InternalBotCommand:
                    if message.command == 'terminate':
                        self.terminated.set()
                    elif message.command == 'fixed':
                        if message.args == 'on':
                            self.fixed_width = True
                        elif message.args == 'off':
                            self.fixed_width = False
                        print(f"self.fixed_width set to: {self.fixed_width}")
                    else:
                        print(f"DiscordConnector: unknown InternalBotCommand {message.command}")
                elif self.authed_member is not None:
                    await self.send(message)
                else:
                    print(f"Not sending message as no registered user populated yet")
                self.bot_out_queue.task_done()
            await self.close()
        except Exception as e:
            print(f"DiscordConnector error in process_bot_outgoing(): {e}")
