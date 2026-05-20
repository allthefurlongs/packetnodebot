import asyncio
import re

import yaml
import discord
import packetnodebot.common


def discord_maxlen_string_chunks(full_message):
    return (full_message[0+i:1900+i] for i in range(0, len(full_message), 1900))


class DiscordConnector(discord.Client):
    def __init__(self, *args, conf, conf_file, terminated, bot_in_queue, bot_out_queue, state, **kwargs,):
        super().__init__(*args, **kwargs)
        self.conf = conf
        self.conf_file = conf_file
        self.terminated = terminated
        self.bot_in_queue = bot_in_queue
        self.bot_out_queue = bot_out_queue
        self.state = state
        self.authed_member = None

    async def on_ready(self):
        print(f'Connected to discord as {self.user} (ID: {self.user.id})')
        if 'sysop_user_id' in self.conf['discord']:
            self.authed_member = await self.fetch_user(self.conf['discord']['sysop_user_id'])
            print(f"Authorised discord user: {self.authed_member}")
        await self.bot_out_queue.put("Bot Online")

    def _fence_for(self, message):
        # Prevent message content containing ``` from injecting into our fixed-width markdown and breaking the
        # formatting: pick a fence longer than any backtick run in the payload so embedded ``` can't close the block
        # early.
        longest_run = max((len(m) for m in re.findall(r'`+', message)), default=0)
        return '`' * max(3, longest_run + 1)

    async def send(self, message, member=None):
        if member is None:
            member = self.authed_member
        if self.state.fixed_width:
            fence = self._fence_for(message)
            await member.send(f"{fence}{message}{fence}")
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
        while not self.terminated.is_set():
            try:
                message = await asyncio.wait_for(self.bot_out_queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            try:
                if type(message) is packetnodebot.common.InternalBotCommand:
                    if message.command == 'terminate':
                        self.terminated.set()
                    else:
                        print(f"DiscordConnector: unknown InternalBotCommand {message.command}")
                elif self.authed_member is not None:
                    # Discord max message length is 2000, or the message is rejected, but in reality it seems to need to
                    # be slightly less. When fixed_width is on, size the fence to the whole message before chunking so
                    # every chunk uses the same fence and no chunk's backtick runs can close the block early.
                    if self.state.fixed_width:
                        fence = self._fence_for(message)
                        chunk_size = 1900 - 2 * len(fence)
                        for i in range(0, len(message), chunk_size):
                            await self.authed_member.send(f"{fence}{message[i:i+chunk_size]}{fence}")
                    elif len(message) > 1900:
                        for message_chunk in discord_maxlen_string_chunks(message):
                            await self.authed_member.send(message_chunk)
                    else:
                        await self.authed_member.send(message)
                else:
                    print(f"Not sending message as no registered user populated yet")
            except Exception as e:
                print(f"DiscordConnector error in process_bot_outgoing(): {e}")
            finally:
                self.bot_out_queue.task_done()
        await self.close()
