# MusicBot

A Discord music bot that supports both slash commands and a traditional prefix (`,`). It can accept Spotify, Apple Music, Tidal, and YouTube links or text search terms, then finds playable audio via YouTube. Playback is controlled with commands and interactive buttons for play/pause, skip, shuffle, and stop.

## Features
- Slash commands (`/play`, `/pause`, `/skip`, `/shuffle`, `/queue`, `/stop`, `/join`, `/leave`) alongside prefix commands (`,play`, `,pause`, etc.).
- Accepts direct YouTube links as well as Spotify, Apple Music, and Tidal URLs by searching YouTube for the matched track.
- Text search support using YouTube search results.
- Interactive control buttons on the now-playing message.
- Simple per-guild music state with queue management and shuffle.

## Getting started
1. Copy `.env.example` to `.env` and fill in your bot credentials:
   ```bash
   cp .env.example .env
   # Edit .env to set DISCORD_TOKEN and any optional IDs
   ```

   Required and optional keys:
   - `DISCORD_TOKEN` (**required**): your bot token from the Discord Developer Portal.
   - `DISCORD_APPLICATION_ID` (optional): your bot/application ID. Set this if you want to explicitly pin the application ID used by slash commands.
   - `GUILD_IDS` (optional): comma-separated guild IDs (e.g., `123,456`). When set, slash commands are synced to those guilds for faster updates during testing.

2. Install dependencies:
   ```bash
   python -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```
3. Run a quick sanity check to ensure the bot file compiles:
   ```bash
   python -m py_compile bot.py
   ```
4. Run the bot:
   ```bash
   python bot.py
   ```

## Requirements
- Python 3.10+
- FFmpeg available on your system for audio playback
- A Discord bot token with the **MESSAGE CONTENT INTENT** enabled (needed for prefix commands)

## Notes on streaming sources
Spotify, Apple Music, and Tidal streams are not played directly. Their links are used to fetch track metadata, and the bot searches YouTube for an equivalent audio stream to play in Discord.
