#  Copyright 2019 Allan Galarza
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#  http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.

import datetime as dt
import logging
import os
import re
import traceback
from collections import defaultdict
from typing import List, Optional, Union

import aiohttp
import asyncpg
import discord
from discord.ext import commands

import cogs.utils.context
from cogs.utils import config
from cogs.utils import safe_delete_message
from cogs.utils.database import get_server_property
from cogs.utils.tibia import populate_worlds, tibia_worlds

initial_cogs = {
    "cogs.core",
    "cogs.serverlog",
    "cogs.tracking",
    "cogs.owner",
    "cogs.mod",
    "cogs.admin",
    "cogs.tibia",
    "cogs.general",
    "cogs.loot",
    "cogs.tibiawiki",
    "cogs.roles",
    "cogs.info",
    "cogs.calculators",
    "cogs.timers"
}

log = logging.getLogger("nabbot")


async def _prefix_callable(bot, msg):
    user_id = bot.user.id
    base = [f'<@!{user_id}> ', f'<@{user_id}> ']
    if msg.guild is None:
        base.extend(bot.config.command_prefix)
    else:
        prefixes = bot.prefixes[msg.guild.id]
        base.extend(prefixes)
    base = sorted(base, reverse=True)
    return base


class NabBot(commands.AutoShardedBot):
    def __init__(self):
        super().__init__(command_prefix=_prefix_callable, case_insensitive=True, fetch_offline_members=True,
                         description="Discord bot with functions for the MMORPG Tibia.")
        # Remove default help command to implement custom one
        self.remove_command("help")

        self.users_servers = defaultdict(list)
        self.config: config.Config = None
        self.pool: asyncpg.pool.Pool = None
        self.start_time = dt.datetime.utcnow()
        self.session = aiohttp.ClientSession(loop=self.loop)
        # Dictionary of worlds tracked by nabbot, key:value = server_id:world
        # Dictionary is populated from database
        # A list version is created from the dictionary
        self.tracked_worlds = {}
        self.tracked_worlds_list = []

        self.prefixes = defaultdict()

        self.__version__ = "2.3.1a"

    async def on_ready(self):
        """Called when the bot is ready."""
        print('Logged in as')
        print(self.user)
        print(self.user.id)
        print(f"Version {self.__version__}")
        print('------')
        # Populating members's guild list
        self.users_servers.clear()
        for guild in self.guilds:
            for member in guild.members:
                self.users_servers[member.id].append(guild.id)
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                records = [(user.id, guild.id) for guild in self.guilds for user in guild.members]
                await conn.execute("TRUNCATE user_server")
                await conn.copy_records_to_table("user_server", columns=["user_id", "server_id"], records=records)
        log.info("Bot is online and ready")

    async def on_message(self, message: discord.Message):
        """Called every time a message is sent on a visible channel."""
        # Ignore if message is from any bot
        if message.author.bot:
            return

        ctx = await self.get_context(message, cls=cogs.utils.context.NabCtx)
        if ctx.command is not None:
            return await self.invoke(ctx)
        # This is a PM, no further info needed
        if message.guild is None:
            return
        if message.content.strip() == f"<@{self.user.id}>":
            prefixes = list(self.config.command_prefix)
            if ctx.guild:
                prefixes = self.prefixes[message.guild.id]
            if prefixes:
                prefixes_str = ", ".join(f"`{p}`" for p in prefixes)
                return await ctx.send(f"My command prefixes are: {prefixes_str}, and mentions. "
                                      f"To see my commands, try: `{prefixes[0]}help.`", delete_after=10)
            else:
                return await ctx.send(f"My command prefix is mentions. "
                                      f"To see my commands, try: `@{self.user.name} help.`", delete_after=10)

        server_delete = await get_server_property(ctx.pool, message.guild.id, "commandsonly")
        global_delete = self.config.ask_channel_delete
        if (server_delete is None and global_delete or server_delete) and await ctx.is_askchannel():
            await safe_delete_message(message)

    # ------------ Utility methods ------------

    def get_member(self, argument: Union[str, int], guild: Union[discord.Guild, List[discord.Guild]] = None) \
            -> Union[discord.Member, discord.User]:
        """Returns a member matching the arguments provided.

        If a guild or guild list is specified, then only members from those guilds will be searched. If no guild is
        specified, the first member instance will be returned.
        :param argument: The argument to search for, can be an id, name#disctriminator, nickname or name
        :param guild: The guild or list of guilds that limit the search.
        :return: The member found or None.
        """
        id_regex = re.compile(r'([0-9]{15,21})$')
        mention_regex = re.compile(r'<@!?([0-9]+)>$')
        match = id_regex.match(str(argument)) or mention_regex.match(str(argument))
        if match is None:
            return self.get_member_named(argument, guild)
        else:
            user_id = int(match.group(1))
            if guild is None:
                return self.get_user(user_id)
            if isinstance(guild, list) and len(guild) == 1:
                guild = guild[0]
            if isinstance(guild, list) and len(guild) > 0:
                members = [m for ml in [g.members for g in guild] for m in ml]
                return discord.utils.find(lambda m: m.id == user_id, members)
            return guild.get_member(user_id)

    def get_member_named(self, name: str, guild: Union[discord.Guild, List[discord.Guild]] = None) -> discord.Member:
        """Returns a member matching the name

        If a guild or guild list is specified, then only members from those guilds will be searched. If no guild is
        specified, the first member instance will be returned.

        :param name: The name, nickname or name#discriminator of the member
        :param guild: The guild or list of guilds to limit the search
        :return: The member found or none
        """
        name = str(name)
        members = self.get_all_members()
        if isinstance(guild, list) and len(guild) == 1:
            guild = guild[0]
        if type(guild) is discord.Guild:
            members = guild.members
        if isinstance(guild, list) and len(guild) > 0:
            members = [m for ml in [g.members for g in guild] for m in ml]
        if len(name) > 5 and name[-5] == '#':
            potential_discriminator = name[-4:]
            result = discord.utils.get(members, name=name[:-5], discriminator=potential_discriminator)
            if result is not None:
                return result
        return discord.utils.find(lambda m: m.display_name.lower() == name.lower() or m.name.lower == name.lower(),
                                  members)

    def get_user_guilds(self, user_id: int) -> List[discord.Guild]:
        """Returns a list of the user's shared guilds with the bot"""
        try:
            return [self.get_guild(gid) for gid in self.users_servers[user_id]]
        except KeyError:
            return []

    def get_guilds_worlds(self, guild_list: List[discord.Guild]) -> List[str]:
        """Returns a list of all tracked worlds found in a list of guilds."""
        return list(set([world for guild, world in self.tracked_worlds.items() if guild in [g.id for g in guild_list]]))

    def get_user_worlds(self, user_id: int) -> List[str]:
        """Returns a list of all the tibia worlds the user is tracked in.

        This is based on the tracked world of each guild the user belongs to.
        guild_list can be passed to search in a specific set of guilds. Note that the user may not belong to them."""
        guild_list = self.get_user_guilds(user_id)
        return self.get_guilds_worlds(guild_list)

    def get_channel_or_top(self, guild: discord.Guild, channel_id: int) -> discord.TextChannel:
        """Returns a guild's channel by id, returns none if channel doesn't exist

        It also checks if the bot has permissions on that channel, if not, it will return the top channel too."""
        if channel_id is None:
            return self.get_top_channel(guild)
        channel = guild.get_channel(int(channel_id))
        if channel is None:
            return self.get_top_channel(guild)
        permissions = channel.permissions_for(guild.me)
        if not permissions.read_messages or not permissions.send_messages:
            return self.get_top_channel(guild)
        return channel

    async def send_log_message(self, guild: discord.Guild, content=None, *, embed: discord.Embed = None):
        """Sends a message on the server-log channel

        If the channel doesn't exist, it doesn't send anything or give of any warnings as it meant to be an optional
        feature."""
        ask_channel_id = await get_server_property(self.pool, guild.id, "serverlog")
        channel = None
        if ask_channel_id:
            channel = guild.get_channel(ask_channel_id)
        if channel is None:
            channel = self.get_channel_by_name(self.config.log_channel_name, guild)
        if channel is None:
            return
        try:
            await channel.send(content=content, embed=embed)
            return True
        except discord.HTTPException:
            return False

    def get_channel_by_name(self, name: str, guild: discord.Guild) -> discord.TextChannel:
        """Finds a channel by name on all the servers the bot is in.

        If guild is specified, only channels in that guild will be searched"""
        if guild is None:
            channel = discord.utils.find(lambda m: m.name == name and not type(m) == discord.ChannelType.voice,
                                         self.get_all_channels())
        else:
            channel = discord.utils.find(lambda m: m.name == name and not type(m) == discord.ChannelType.voice,
                                         guild.channels)
        return channel

    def get_guild_by_name(self, name: str) -> discord.Guild:
        """Returns a guild by its name"""

        guild = discord.utils.find(lambda m: m.name.lower() == name.lower(), self.guilds)
        return guild

    @staticmethod
    def get_top_channel(guild: discord.Guild) -> Optional[discord.TextChannel]:
        """Returns the highest text channel on the list.

        If writeable_only is set, the first channel where the bot can write is returned
        If None it returned, the guild has no channels or the bot can't write on any channel"""
        if guild is None:
            return None
        for channel in guild.text_channels:
            if channel.permissions_for(guild.me).send_messages:
                return channel
        return None

    async def reload_worlds(self):
        """Refresh the world list from the database

        This is used to avoid reading the database every time the world list is needed.
        A global variable holding the world list is loaded on startup and refreshed only when worlds are modified"""
        tibia_servers_dict_temp = {}
        rows = await self.pool.fetch("SELECT server_id, value FROM server_property WHERE key = $1 ORDER BY value ASC",
                                     "world")
        del self.tracked_worlds_list[:]
        if len(rows) > 0:
            for row in rows:
                value = row["value"]
                if value not in self.tracked_worlds_list:
                    self.tracked_worlds_list.append(value)
                tibia_servers_dict_temp[int(row["server_id"])] = value

        self.tracked_worlds.clear()
        self.tracked_worlds.update(tibia_servers_dict_temp)

    async def load_prefixes(self):
        """Populates the prefix mapping."""
        rows = await self.pool.fetch("SELECT server_id, prefixes FROM server_prefixes")
        for row in rows:
            self.prefixes[row['server_id']] = row['prefixes']

    def run(self):
        print("Loading config...")
        config.parse()
        self.config = config
        self.prefixes = defaultdict(lambda: list(config.command_prefix))

        # List of tracked worlds for NabBot
        self.loop.run_until_complete(self.reload_worlds())
        # List of all Tibia worlds
        self.loop.run_until_complete(populate_worlds())
        # Load prefixes
        self.loop.run_until_complete(self.load_prefixes())

        if len(tibia_worlds) == 0:
            print("Critical information was not available: NabBot can not start without the World List.")
            quit()
        token = get_token()

        print("Loading cogs...")
        for cog in initial_cogs:
            try:
                self.load_extension(cog)
                print(f"Cog {cog} loaded successfully.")
            except ModuleNotFoundError:
                print(f"Could not find cog: {cog}")
            except Exception:
                print(f'Cog {cog} failed to load:')
                traceback.print_exc(limit=-1)
                log.exception(f'Cog {cog} failed to load')

        for extra in config.extra_cogs:
            try:
                self.load_extension(extra)
                print(f"Extra cog {extra} loaded successfully.")
            except ModuleNotFoundError:
                print(f"Could not find extra cog: {extra}")
            except Exception:
                print(f'Extra cog {extra} failed to load:')
                traceback.print_exc(limit=-1)
                log.exception(f'Extra cog {extra} failed to load:')

        try:
            print("Attempting login...")
            super().run(token)
        except discord.errors.LoginFailure:
            print("Invalid token. Edit token.txt to fix it.")
            input("\nPress any key to continue...")
            quit()


def get_token():
    """When the bot is run without a login.py file, it prompts the user for login info"""
    if not os.path.isfile("token.txt"):
        print("This seems to be the first time NabBot is ran (or token.txt is missing)")
        print("To run your own instance of NabBot you need to create a new bot account to get a bot token")
        print("https://discordapp.com/developers/applications/me")
        print("Enter the token:")
        token = input(">>")
        if len(token) < 50:
            input("What you entered isn't a token. Restart NabBot to retry.")
            quit()
        with open("token.txt", "w+") as f:
            f.write(token)
        print("Token has been saved to token.txt, you can edit this file later to change it.")
        input("Press any key to start NabBot now...")
        return token
    else:
        with open("token.txt") as f:
            return f.read().strip()


if __name__ == "__main__":
    print("NabBot can't be started from this file anymore. Use launcher.py.")
