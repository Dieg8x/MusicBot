import asyncio
import os
import re
from dataclasses import dataclass, field
from typing import Iterable, Optional, Union

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import find_dotenv, load_dotenv
import yt_dlp

AUDIO_FORMAT = "bestaudio/best"
YDL_OPTS = {
    "format": AUDIO_FORMAT,
    "noplaylist": True,
    "quiet": True,
}
FFMPEG_OPTIONS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
}

SPOTIFY_RE = re.compile(r"https?://(open\.)?spotify\.com/")
APPLE_RE = re.compile(r"https?://music\.apple\.com/")
TIDAL_RE = re.compile(r"https?://(www\.)?tidal\.com/")
YOUTUBE_RE = re.compile(r"https?://(www\.)?(youtube\.com|youtu\.be)/")


@dataclass
class Track:
    title: str
    url: str


@dataclass
class MusicState:
    voice_client: Optional[discord.VoiceClient] = None
    queue: list[Track] = field(default_factory=list)
    now_playing: Optional[Track] = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


@dataclass
class Settings:
    token: str
    application_id: Optional[int] = None
    test_guild_ids: tuple[int, ...] = ()
    shard_count: Optional[int] = None


def _parse_int_list(raw: Optional[str]) -> tuple[int, ...]:
    if not raw:
        return ()
    values: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            values.append(int(part))
        except ValueError:
            raise RuntimeError(
                f"GUILD_IDS must be a comma-separated list of integers; got '{part}'."
            )
    return tuple(values)


def load_env_file() -> None:
    env_path = find_dotenv()
    if env_path:
        load_dotenv(env_path)


def load_settings() -> Settings:
    load_env_file()

    token = os.getenv("DISCORD_TOKEN", "").strip()
    placeholder_tokens = {
        "your_bot_token_here",
        "discord_token_here",
        "paste_token_here",
        "placeholder",
    }
    if not token or token.lower() in placeholder_tokens:
        raise RuntimeError(
            "DISCORD_TOKEN is required. Set it as an environment variable or add it to a .env file as DISCORD_TOKEN=YOUR_TOKEN."
        )

    application_id = os.getenv("DISCORD_APPLICATION_ID")
    app_id_int = int(application_id) if application_id else None

    guild_ids = _parse_int_list(os.getenv("GUILD_IDS"))

    shard_count_raw = os.getenv("SHARD_COUNT")
    shard_count = int(shard_count_raw) if shard_count_raw else None

    return Settings(
        token=token,
        application_id=app_id_int,
        test_guild_ids=guild_ids,
        shard_count=shard_count,
    )


settings = load_settings()

intents = discord.Intents.default()
intents.message_content = True
bot = commands.AutoShardedBot(
    command_prefix=",",
    intents=intents,
    application_id=settings.application_id,
    shard_count=settings.shard_count,
)
music_states: dict[int, MusicState] = {}


def get_state(guild_id: int) -> MusicState:
    state = music_states.get(guild_id)
    if not state:
        state = MusicState()
        music_states[guild_id] = state
    return state


async def search_youtube(query: str) -> Optional[Track]:
    loop = asyncio.get_running_loop()

    def _extract():
        with yt_dlp.YoutubeDL(YDL_OPTS) as ydl:
            return ydl.extract_info(f"ytsearch1:{query}", download=False)

    data = await loop.run_in_executor(None, _extract)
    if not data:
        return None
    entry = data["entries"][0]
    return Track(title=entry.get("title", query), url=entry.get("webpage_url"))


async def resolve_link(query: str) -> Optional[Track]:
    # Direct YouTube link
    if YOUTUBE_RE.match(query):
        loop = asyncio.get_running_loop()

        def _extract():
            with yt_dlp.YoutubeDL(YDL_OPTS) as ydl:
                return ydl.extract_info(query, download=False)

        info = await loop.run_in_executor(None, _extract)
        if info:
            return Track(title=info.get("title", query), url=info.get("webpage_url", query))
        return None

    # Other music services: fetch metadata and search YouTube for playback
    if SPOTIFY_RE.match(query) or APPLE_RE.match(query) or TIDAL_RE.match(query):
        loop = asyncio.get_running_loop()

        def _extract_meta():
            with yt_dlp.YoutubeDL({**YDL_OPTS, "skip_download": True}) as ydl:
                return ydl.extract_info(query, download=False)

        metadata = await loop.run_in_executor(None, _extract_meta)
        if not metadata:
            return None
        artists = metadata.get("artist") or metadata.get("artists")
        artist_name = ", ".join(artists) if isinstance(artists, list) else artists or ""
        title = metadata.get("title") or metadata.get("track") or query
        search_term = f"{artist_name} - {title}".strip(" -")
        return await search_youtube(search_term)

    return None


async def resolve_track(query: str) -> Optional[Track]:
    if re.match(r"https?://", query):
        track = await resolve_link(query)
        if track:
            return track
    return await search_youtube(query)


async def get_stream_url(url: str) -> Optional[str]:
    loop = asyncio.get_running_loop()

    def _extract():
        with yt_dlp.YoutubeDL({**YDL_OPTS, "format": AUDIO_FORMAT}) as ydl:
            info = ydl.extract_info(url, download=False)
            return info.get("url") if info else None

    return await loop.run_in_executor(None, _extract)


async def ensure_voice(channel: discord.VoiceChannel, state: MusicState) -> discord.VoiceClient:
    if state.voice_client and state.voice_client.is_connected():
        return state.voice_client
    voice = await channel.connect()
    state.voice_client = voice
    return voice


async def play_next(guild_id: int):
    state = get_state(guild_id)
    if not state.queue or not state.voice_client:
        state.now_playing = None
        return

    async with state.lock:
        if state.voice_client.is_playing() or state.voice_client.is_paused():
            return
        track = state.queue.pop(0)
        state.now_playing = track
        stream_url = await get_stream_url(track.url)
        if not stream_url:
            state.now_playing = None
            return

        source = discord.FFmpegPCMAudio(stream_url, **FFMPEG_OPTIONS)
        state.voice_client.play(
            discord.PCMVolumeTransformer(source),
            after=lambda err: asyncio.run_coroutine_threadsafe(play_next(guild_id), bot.loop),
        )


async def send_reply(target: Union[commands.Context, discord.Interaction], **kwargs):
    if isinstance(target, discord.Interaction):
        if target.response.is_done():
            return await target.followup.send(**kwargs)
        return await target.response.send_message(**kwargs)
    return await target.send(**kwargs)


async def ensure_connected(target: Union[commands.Context, discord.Interaction]) -> Optional[discord.VoiceChannel]:
    user = target.user if isinstance(target, discord.Interaction) else target.author
    if not user or not user.voice or not user.voice.channel:
        await send_reply(target, content="You need to join a voice channel first.", ephemeral=isinstance(target, discord.Interaction))
        return None
    return user.voice.channel


async def handle_play(target: Union[commands.Context, discord.Interaction], query: str):
    if not target.guild:
        return await send_reply(target, content="This command can only be used in a server.")

    channel = await ensure_connected(target)
    if not channel:
        return

    state = get_state(target.guild.id)
    track = await resolve_track(query)
    if not track:
        return await send_reply(target, content="Could not find a matching track.")

    await ensure_voice(channel, state)
    state.queue.append(track)
    await send_reply(target, content=f"Queued **{track.title}**.")

    if not state.voice_client.is_playing() and not state.voice_client.is_paused():
        await play_next(target.guild.id)
        await send_now_playing(target.guild.id, target)


async def send_now_playing(guild_id: int, target: Union[commands.Context, discord.Interaction]):
    state = get_state(guild_id)
    if not state.now_playing:
        return

    embed = discord.Embed(title="Now playing", description=state.now_playing.title, color=discord.Color.blurple())
    view = ControlView(guild_id)
    await send_reply(target, embed=embed, view=view)


class ControlView(discord.ui.View):
    def __init__(self, guild_id: int):
        super().__init__(timeout=180)
        self.guild_id = guild_id

    @discord.ui.button(label="Pause/Resume", style=discord.ButtonStyle.primary)
    async def pause_resume(self, interaction: discord.Interaction, _: discord.ui.Button):
        state = get_state(self.guild_id)
        vc = state.voice_client
        if not vc:
            return await interaction.response.send_message("Not connected.", ephemeral=True)
        if vc.is_paused():
            vc.resume()
            await interaction.response.send_message("Resumed playback.", ephemeral=True)
        elif vc.is_playing():
            vc.pause()
            await interaction.response.send_message("Paused playback.", ephemeral=True)
        else:
            await interaction.response.send_message("Nothing is playing.", ephemeral=True)

    @discord.ui.button(label="Skip", style=discord.ButtonStyle.secondary)
    async def skip(self, interaction: discord.Interaction, _: discord.ui.Button):
        state = get_state(self.guild_id)
        vc = state.voice_client
        if not vc or not (vc.is_playing() or vc.is_paused()):
            return await interaction.response.send_message("Nothing to skip.", ephemeral=True)
        vc.stop()
        await interaction.response.send_message("Skipped track.", ephemeral=True)

    @discord.ui.button(label="Shuffle", style=discord.ButtonStyle.success)
    async def shuffle(self, interaction: discord.Interaction, _: discord.ui.Button):
        state = get_state(self.guild_id)
        if not state.queue:
            return await interaction.response.send_message("Queue is empty.", ephemeral=True)
        import random

        random.shuffle(state.queue)
        await interaction.response.send_message("Shuffled the queue.", ephemeral=True)

    @discord.ui.button(label="Stop", style=discord.ButtonStyle.danger)
    async def stop(self, interaction: discord.Interaction, _: discord.ui.Button):
        state = get_state(self.guild_id)
        if state.voice_client:
            state.queue.clear()
            state.voice_client.stop()
            await interaction.response.send_message("Stopped playback and cleared the queue.", ephemeral=True)
        else:
            await interaction.response.send_message("Not connected.", ephemeral=True)


async def show_queue(target: Union[commands.Context, discord.Interaction]):
    state = get_state(target.guild.id)
    if not state.queue:
        return await send_reply(target, content="Queue is empty.")
    lines = [f"{idx+1}. {track.title}" for idx, track in enumerate(state.queue)]
    embed = discord.Embed(title="Queue", description="\n".join(lines[:15]), color=discord.Color.green())
    await send_reply(target, embed=embed)


async def join_channel(target: Union[commands.Context, discord.Interaction]):
    channel = await ensure_connected(target)
    if not channel:
        return
    state = get_state(target.guild.id)
    await ensure_voice(channel, state)
    await send_reply(target, content=f"Joined **{channel.name}**.")


async def leave_channel(target: Union[commands.Context, discord.Interaction]):
    state = get_state(target.guild.id)
    if state.voice_client:
        await state.voice_client.disconnect()
        state.voice_client = None
        state.queue.clear()
        state.now_playing = None
        await send_reply(target, content="Disconnected and cleared the queue.")
    else:
        await send_reply(target, content="Not connected to a voice channel.")


# Prefix commands
@bot.command(name="play")
async def play_cmd(ctx: commands.Context, *, query: str):
    await handle_play(ctx, query)


@bot.command(name="pause")
async def pause_cmd(ctx: commands.Context):
    state = get_state(ctx.guild.id)
    vc = state.voice_client
    if vc and vc.is_playing():
        vc.pause()
        await ctx.send("Paused playback.")
    else:
        await ctx.send("Nothing is playing.")


@bot.command(name="resume")
async def resume_cmd(ctx: commands.Context):
    state = get_state(ctx.guild.id)
    vc = state.voice_client
    if vc and vc.is_paused():
        vc.resume()
        await ctx.send("Resumed playback.")
    else:
        await ctx.send("Nothing to resume.")


@bot.command(name="skip")
async def skip_cmd(ctx: commands.Context):
    state = get_state(ctx.guild.id)
    if state.voice_client and (state.voice_client.is_playing() or state.voice_client.is_paused()):
        state.voice_client.stop()
        await ctx.send("Skipped.")
    else:
        await ctx.send("Nothing to skip.")


@bot.command(name="queue")
async def queue_cmd(ctx: commands.Context):
    await show_queue(ctx)


@bot.command(name="shuffle")
async def shuffle_cmd(ctx: commands.Context):
    state = get_state(ctx.guild.id)
    if not state.queue:
        return await ctx.send("Queue is empty.")
    import random

    random.shuffle(state.queue)
    await ctx.send("Shuffled the queue.")


@bot.command(name="stop")
async def stop_cmd(ctx: commands.Context):
    state = get_state(ctx.guild.id)
    if state.voice_client:
        state.queue.clear()
        state.voice_client.stop()
        await ctx.send("Stopped playback and cleared the queue.")
    else:
        await ctx.send("Not connected.")


@bot.command(name="join")
async def join_cmd(ctx: commands.Context):
    await join_channel(ctx)


@bot.command(name="leave")
async def leave_cmd(ctx: commands.Context):
    await leave_channel(ctx)


# Slash commands
@bot.tree.command(name="play", description="Play a track from a link or search query.")
@app_commands.describe(query="YouTube link or search term; Spotify/Apple Music/Tidal links are also accepted.")
async def play_slash(interaction: discord.Interaction, query: str):
    await handle_play(interaction, query)


@bot.tree.command(name="pause", description="Pause the current track.")
async def pause_slash(interaction: discord.Interaction):
    state = get_state(interaction.guild.id)
    vc = state.voice_client
    if vc and vc.is_playing():
        vc.pause()
        await interaction.response.send_message("Paused playback.", ephemeral=True)
    else:
        await interaction.response.send_message("Nothing is playing.", ephemeral=True)


@bot.tree.command(name="resume", description="Resume playback.")
async def resume_slash(interaction: discord.Interaction):
    state = get_state(interaction.guild.id)
    vc = state.voice_client
    if vc and vc.is_paused():
        vc.resume()
        await interaction.response.send_message("Resumed playback.", ephemeral=True)
    else:
        await interaction.response.send_message("Nothing to resume.", ephemeral=True)


@bot.tree.command(name="skip", description="Skip the current track.")
async def skip_slash(interaction: discord.Interaction):
    state = get_state(interaction.guild.id)
    if state.voice_client and (state.voice_client.is_playing() or state.voice_client.is_paused()):
        state.voice_client.stop()
        await interaction.response.send_message("Skipped.")
    else:
        await interaction.response.send_message("Nothing to skip.", ephemeral=True)


@bot.tree.command(name="queue", description="Show the current queue.")
async def queue_slash(interaction: discord.Interaction):
    await show_queue(interaction)


@bot.tree.command(name="shuffle", description="Shuffle the queue.")
async def shuffle_slash(interaction: discord.Interaction):
    state = get_state(interaction.guild.id)
    if not state.queue:
        return await interaction.response.send_message("Queue is empty.", ephemeral=True)
    import random

    random.shuffle(state.queue)
    await interaction.response.send_message("Shuffled the queue.")


@bot.tree.command(name="stop", description="Stop playback and clear the queue.")
async def stop_slash(interaction: discord.Interaction):
    state = get_state(interaction.guild.id)
    if state.voice_client:
        state.queue.clear()
        state.voice_client.stop()
        await interaction.response.send_message("Stopped playback and cleared the queue.")
    else:
        await interaction.response.send_message("Not connected.", ephemeral=True)


@bot.tree.command(name="join", description="Join your current voice channel.")
async def join_slash(interaction: discord.Interaction):
    await join_channel(interaction)


@bot.tree.command(name="leave", description="Disconnect the bot from voice.")
async def leave_slash(interaction: discord.Interaction):
    await leave_channel(interaction)


@bot.event
async def on_ready():
    if settings.test_guild_ids:
        for guild_id in settings.test_guild_ids:
            guild_obj = discord.Object(id=guild_id)
            await bot.tree.sync(guild=guild_obj)
        registered: Iterable[discord.AppCommand] = bot.tree.get_commands(guild=None)
        print(
            f"Registered {len(registered)} global commands and synced guild-specific commands for {settings.test_guild_ids}."
        )
    else:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} global commands.")

    shard_info = (
        f" across {bot.shard_count} shard(s)" if bot.shard_count else ""
    )
    print(f"Logged in as {bot.user} (ID: {bot.user.id}){shard_info}")


def main():
    bot.run(settings.token)


if __name__ == "__main__":
    main()
