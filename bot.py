import os
import re
import asyncio
import random
from datetime import datetime, timedelta
import discord
from discord.ext import commands

intents = discord.Intents.default()
intents.guilds = True
intents.messages = True
intents.message_content = True
intents.guild_message_reactions = True
intents.dm_messages = True

bot = commands.Bot(command_prefix="!", intents=intents)

# Active giveaways store
# Structure: message_id -> { channel_id, guild_id, prize, end_time, winners_count, host_id, manual_winner_id, participants: set }
giveaways = {}

def parse_duration(duration_str: str) -> int:
    match = re.match(r"^(\d+)([mhd])$", duration_str.strip(), re.IGNORECASE)
    if not match:
        return None
    amount = int(match.group(1))
    unit = match.group(2).lower()
    if unit == 'm':
        return amount * 60
    elif unit == 'h':
        return amount * 3600
    elif unit == 'd':
        return amount * 86400
    return None

@bot.event
async def on_ready():
    print(f"[SYS] Logged in as {bot.user} (ID: {bot.user.id})")
    try:
        synced = await bot.tree.sync()
        print(f"[SYS] Synced {len(synced)} slash commands.")
    except Exception as e:
        print(f"[SYS] Failed to sync commands: {e}")

@bot.tree.command(name="gstart", description="Start a new professional giveaway")
@discord.app_commands.default_permissions(administrator=True)
@discord.app_commands.describe(
    duration="Duration of giveaway (e.g. 10m, 2h, 1d)",
    winners="Number of winners to select",
    prize="Prize for the giveaway"
)
async def gstart(interaction: discord.Interaction, duration: str, winners: int, prize: str):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ You require **Administrator** permissions to start a giveaway.", ephemeral=True)
        return

    duration_seconds = parse_duration(duration)
    if not duration_seconds:
        await interaction.response.send_message("❌ Invalid duration format! Use `10m` for 10 minutes, `2h` for 2 hours, or `1d` for 1 day.", ephemeral=True)
        return

    end_time_dt = datetime.utcnow() + timedelta(seconds=duration_seconds)
    end_timestamp = int(end_time_dt.timestamp())

    embed = discord.Embed(
        title=f"🎉 GIVEAWAY: {prize}",
        description=f"React with 🎉 to enter!\n\n**Winners:** {winners}\n**Hosted By:** {interaction.user.mention}\n**Ends:** <t:{end_timestamp}:R>",
        color=discord.Color.from_str("#5865F2")
    )
    embed.set_footer(text="Giveaways • React 🎉 to enter")
    embed.timestamp = end_time_dt

    await interaction.response.send_message("🎉 Giveaway starting...", ephemeral=True)
    message = await interaction.channel.send(embed=embed)
    await message.add_reaction("🎉")

    giveaway_data = {
        "message_id": message.id,
        "channel_id": interaction.channel.id,
        "guild_id": interaction.guild.id,
        "prize": prize,
        "end_time": end_timestamp,
        "winners_count": winners,
        "host_id": interaction.user.id,
        "manual_winner_id": None,
        "participants": set()
    }

    giveaways[message.id] = giveaway_data

    # Send DM instructions to host
    try:
        dm_embed = discord.Embed(
            title="🎉 Giveaway Started!",
            description=f"Your giveaway for **{prize}** has officially started in {interaction.channel.mention}.",
            color=discord.Color.from_str("#57F287")
        )
        dm_embed.add_field(name="Giveaway ID", value=f"`{message.id}`", inline=True)
        dm_embed.add_field(
            name="How to force a winner",
            value=f"Reply to this DM with:\n`!gwin {message.id} <USER_ID>`\nOR simply:\n`!gwin <USER_ID>` (applies to your latest active giveaway)",
            inline=False
        )
        await interaction.user.send(embed=dm_embed)
    except discord.Forbidden:
        pass

    # Schedule background task to end giveaway
    bot.loop.create_task(run_giveaway_timer(message.id, duration_seconds))

async def run_giveaway_timer(message_id: int, duration_seconds: int):
    await asyncio.sleep(duration_seconds)
    await end_giveaway(message_id)

async def end_giveaway(message_id: int):
    if message_id not in giveaways:
        return
    
    g_data = giveaways.pop(message_id)
    try:
        channel = bot.get_channel(g_data["channel_id"]) or await bot.fetch_channel(g_data["channel_id"])
        message = await channel.fetch_message(g_data["message_id"])
    except Exception:
        return

    winners = []

    # Priority 1: Manual winner chosen via DM
    if g_data["manual_winner_id"]:
        winners.append(f"<@{g_data['manual_winner_id']}>")

    # Priority 2: Random pick for remaining slots
    remaining_count = g_data["winners_count"] - len(winners)
    if remaining_count > 0 and g_data["participants"]:
        pool = [uid for uid in g_data["participants"] if uid != g_data["manual_winner_id"]]
        for _ in range(min(remaining_count, len(pool))):
            picked = random.choice(pool)
            pool.remove(picked)
            winners.append(f"<@{picked}>")

    ended_embed = discord.Embed(
        title=f"🎉 GIVEAWAY ENDED: {g_data['prize']}",
        description=f"**Winner(s):** {', '.join(winners) if winners else 'No valid entrants'}\n**Hosted By:** <@{g_data['host_id']}>",
        color=discord.Color.from_str("#2F3136")
    )
    ended_embed.set_footer(text="Giveaway Ended")
    ended_embed.timestamp = datetime.utcnow()

    await message.edit(embed=ended_embed)

    if winners:
        await channel.send(f"🎉 Congratulations {', '.join(winners)}! You won **{g_data['prize']}**!")
    else:
        await channel.send(f"Giveaway ended for **{g_data['prize']}**, but no valid participants entered.")

# DM Handler for manual winner selection (!gwin)
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    if message.guild is not None:
        return  # Process DMs only

    if message.content.startswith("!gwin"):
        args = message.content[5:].strip().split()
        if not args:
            await message.channel.send("❌ Usage: `!gwin <User_ID>` or `!gwin <Giveaway_Message_ID> <User_ID>`")
            return

        target_message_id = None
        selected_winner_id = None

        if len(args) == 1:
            selected_winner_id = args[0]
            # Find latest active giveaway hosted by this user
            current_time = int(datetime.utcnow().timestamp())
            for msg_id, g_data in giveaways.items():
                if g_data["host_id"] == message.author.id and current_time < g_data["end_time"]:
                    target_message_id = msg_id
        else:
            target_message_id = int(args[0])
            selected_winner_id = args[1]

        if not target_message_id or target_message_id not in giveaways:
            await message.channel.send("❌ Active giveaway not found or you are not the host.")
            return

        g_data = giveaways[target_message_id]
        if g_data["host_id"] != message.author.id:
            await message.channel.send("❌ You can only select winners for giveaways you hosted.")
            return

        g_data["manual_winner_id"] = selected_winner_id
        giveaways[target_message_id] = g_data

        await message.channel.send(f"✅ Successfully set <@{selected_winner_id}> (`{selected_winner_id}`) as the predetermined winner for giveaway ID `{target_message_id}` ({g_data['prize']}).")

    await bot.process_commands(message)

# Reaction Entry/Exit tracking
@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    if payload.user_id == bot.user.id:
        return
    if payload.message_id not in giveaways:
        return
    if str(payload.emoji) != "🎉":
        return

    g_data = giveaways[payload.message_id]
    g_data["participants"].add(payload.user_id)

    try:
        user = bot.get_user(payload.user_id) or await bot.fetch_user(payload.user_id)
        embed = discord.Embed(
            title="🎉 Entry Confirmed!",
            description=f"You have successfully entered the giveaway for **{g_data['prize']}**!",
            color=discord.Color.from_str("#57F287")
        )
        await user.send(embed=embed)
    except Exception:
        pass

@bot.event
async def on_raw_reaction_remove(payload: discord.RawReactionActionEvent):
    if payload.user_id == bot.user.id:
        return
    if payload.message_id not in giveaways:
        return
    if str(payload.emoji) != "🎉":
        return

    g_data = giveaways[payload.message_id]
    g_data["participants"].discard(payload.user_id)

    try:
        user = bot.get_user(payload.user_id) or await bot.fetch_user(payload.user_id)
        embed = discord.Embed(
            title="❌ Entry Removed",
            description=f"You left the giveaway for **{g_data['prize']}**.",
            color=discord.Color.from_str("#ED4245")
        )
        await user.send(embed=embed)
    except Exception:
        pass

if __name__ == "__main__":
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        print("[ERR] DISCORD_TOKEN environment variable not set.")
    else:
        bot.run(token)
