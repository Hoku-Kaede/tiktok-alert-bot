from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

from tiktok_client import LiveStream, TikTokClient, TikTokError
from watchlist import WatchTarget, Watchlist

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
CHECK_INTERVAL_MINUTES = max(2, int(os.getenv("CHECK_INTERVAL_MINUTES", "5")))
TIKTOK_PROXY = os.getenv("TIKTOK_PROXY", "").strip() or None
STATE_FILE = Path(__file__).resolve().parent / "watchlist.json"
USERNAME_RE = re.compile(r"^[A-Za-z0-9._]{2,24}$")

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
log = logging.getLogger("tiktok-alert-bot")

TIKTOK_CYAN = 0x25F4EE
LIVE_RED = 0xFE2C55

intents = discord.Intents.default()
intents.guilds = True
bot = commands.Bot(command_prefix="!", intents=intents)

client = TikTokClient(proxy=TIKTOK_PROXY)
watchlist = Watchlist.load(STATE_FILE)


def _clean_username(username: str) -> str:
    username = username.strip().lstrip("@")
    if not USERNAME_RE.match(username):
        raise app_commands.AppCommandError(
            "That doesn't look like a valid TikTok username (2-24 letters, numbers, dots, underscores or hyphens)."
        )
    return username


def _post_embed(post) -> discord.Embed:
    embed = discord.Embed(
        title="New TikTok post",
        description=(post.desc or "*no caption*")[:400],
        url=post.url,
        color=TIKTOK_CYAN,
        timestamp=datetime.fromtimestamp(post.create_time, tz=timezone.utc)
        if post.create_time
        else discord.utils.MISSING,
    )
    embed.set_author(name=f"@{post.username}", url=f"https://www.tiktok.com/@{post.username}")
    if post.cover_url:
        embed.set_thumbnail(url=post.cover_url)
    embed.set_footer(text="TikTok • new post")
    return embed


def _live_embed(stream) -> discord.Embed:
    embed = discord.Embed(
        title=f"@{stream.username} is LIVE",
        description=(stream.title or "Started streaming on TikTok")[:400],
        url=stream.url,
        color=LIVE_RED,
    )
    if stream.viewer_count is not None:
        embed.add_field(name="Watching now", value=str(stream.viewer_count), inline=True)
    embed.set_author(name=f"@{stream.username}", url=f"https://www.tiktok.com/@{stream.username}")
    embed.set_footer(text="TikTok • live now")
    return embed


def _check_posts(target: WatchTarget) -> list:
    posts = client.get_recent_posts(target.username, count=12)
    if not posts:
        return []
    newest = posts[0]
    if target.last_post_id is None:
        target.last_post_id = newest.aweme_id
        watchlist.save()
        log.info("Seeded posts for @%s at %s", target.username, newest.aweme_id)
        return []
    if newest.aweme_id == target.last_post_id:
        return []
    seen: set[str] = set()
    fresh = []
    for post in posts:
        if post.aweme_id in seen or post.aweme_id == target.last_post_id:
            break
        seen.add(post.aweme_id)
        fresh.append(post)
    if fresh:
        target.last_post_id = fresh[0].aweme_id
        watchlist.save()
    return fresh[:3]


def _check_live(target: WatchTarget) -> LiveStream | None:
    stream = client.get_live_stream(target.username)
    if stream is None:
        if target.live_room_id is not None:
            target.live_room_id = None
            watchlist.save()
        return None
    if target.live_room_id == stream.room_id:
        return None
    target.live_room_id = stream.room_id
    watchlist.save()
    return stream


async def run_target_check(target: WatchTarget) -> None:
    posts = None
    live = None

    if target.notify_posts:
        try:
            posts = await asyncio.to_thread(_check_posts, target)
        except TikTokError as exc:
            target.consecutive_errors += 1
            watchlist.save()
            log.warning(
                "Post check failed for @%s (%s tries): %s",
                target.username,
                target.consecutive_errors,
                exc,
            )
    if target.notify_live:
        try:
            live = await asyncio.to_thread(_check_live, target)
        except TikTokError as exc:
            target.consecutive_errors += 1
            watchlist.save()
            log.warning(
                "Live check failed for @%s (%s tries): %s",
                target.username,
                target.consecutive_errors,
                exc,
            )
    if not posts and live is None:
        return

    channel = bot.get_channel(target.channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(target.channel_id)
        except discord.DiscordException:
            log.warning(
                "Channel %s missing for @%s, skipping", target.channel_id, target.username
            )
            return

    if live is not None:
        try:
            await channel.send(embed=_live_embed(live))
        except discord.DiscordException as exc:
            log.warning("Failed to send live alert: %s", exc)
    for post in posts or []:
        try:
            await channel.send(embed=_post_embed(post))
        except discord.DiscordException as exc:
            log.warning("Failed to send post alert: %s", exc)


@tasks.loop(minutes=CHECK_INTERVAL_MINUTES)
async def check_all_targets():
    targets = watchlist.all()
    if not targets:
        return
    log.info("Checking %d TikTok target(s)", len(targets))
    for target in targets:
        await run_target_check(target)
        await asyncio.sleep(2)


@check_all_targets.before_loop
async def before_check_all_targets():
    await bot.wait_until_ready()


@bot.event
async def on_ready():
    log.info(
        "Logged in as %s (watching %d target(s), proxy: %s)",
        bot.user,
        len(watchlist.all()),
        "on" if TIKTOK_PROXY else "off",
    )
    if not check_all_targets.is_running():
        check_all_targets.start()


@bot.tree.command(name="watch", description="Watch a TikTok account for new posts and/or live streams")
@app_commands.describe(
    username="TikTok username without @",
    notify_posts="Alert when they post a new video/photo",
    notify_live="Alert when they go live",
    channel="Channel to send alerts to (defaults to this channel)",
)
async def watch(
    interaction: discord.Interaction,
    username: str,
    notify_posts: bool = True,
    notify_live: bool = True,
    channel: discord.TextChannel | None = None,
):
    try:
        username = _clean_username(username)
    except app_commands.AppCommandError as exc:
        await interaction.response.send_message(str(exc), ephemeral=True)
        return
    if not notify_posts and not notify_live:
        await interaction.response.send_message(
            "Pick at least one of posts/live alerts.", ephemeral=True
        )
        return

    target_channel = channel or (
        interaction.channel
        if isinstance(interaction.channel, discord.TextChannel)
        else None
    )
    if target_channel is None:
        await interaction.response.send_message(
            "Use this command in a text channel or pass a `channel` option.", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)

    def probe():
        last_post = None
        stream = None
        if notify_posts:
            posts = client.get_recent_posts(username, count=1)
            last_post = posts[0].aweme_id if posts else None
        if notify_live:
            stream = client.get_live_stream(username)
        return last_post, stream

    try:
        last_post_id, stream = await asyncio.to_thread(probe)
    except TikTokError as exc:
        await interaction.followup.send(f"Couldn't check TikTok: {exc}", ephemeral=True)
        return

    target = WatchTarget(
        username=username,
        channel_id=target_channel.id,
        guild_id=interaction.guild_id or 0,
        notify_posts=notify_posts,
        notify_live=notify_live,
        last_post_id=last_post_id,
        live_room_id=stream.room_id if stream else None,
    )
    watchlist.add(target)
    await interaction.followup.send(
        f"Now watching **@{username}** in {target_channel.mention} "
        f"(posts: {'on' if notify_posts else 'off'}, live: {'on' if notify_live else 'off'}).",
        ephemeral=True,
    )


@bot.tree.command(name="unwatch", description="Stop watching a TikTok account")
@app_commands.describe(username="TikTok username without @")
async def unwatch(interaction: discord.Interaction, username: str):
    username = username.strip().lstrip("@")
    removed = watchlist.remove(interaction.guild_id or 0, username)
    if removed:
        await interaction.response.send_message(f"Stopped watching **@{username}**.", ephemeral=True)
    else:
        await interaction.response.send_message(f"**@{username}** isn't being watched.", ephemeral=True)


@bot.tree.command(name="watchlist", description="List TikTok accounts this server is watching")
async def watchlist_cmd(interaction: discord.Interaction):
    targets = watchlist.for_guild(interaction.guild_id or 0)
    if not targets:
        await interaction.response.send_message("Nothing is being watched yet. Use `/watch`.", ephemeral=True)
        return
    embed = discord.Embed(title="Watched TikTok accounts", color=TIKTOK_CYAN)
    for t in targets:
        features = ", ".join(
            name
            for name, on in (("posts", t.notify_posts), ("live", t.notify_live))
            if on
        )
        channel = bot.get_channel(t.channel_id)
        embed.add_field(
            name=f"@{t.username}",
            value=f"{features} → {channel.mention if channel else t.channel_id}",
            inline=False,
        )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="check", description="Check a watched TikTok account right now")
@app_commands.describe(username="TikTok username without @")
async def check_now(interaction: discord.Interaction, username: str):
    username = username.strip().lstrip("@")
    target = watchlist.get(interaction.guild_id or 0, username)
    if target is None:
        await interaction.response.send_message(f"**@{username}** isn't being watched.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    await run_target_check(target)
    await interaction.followup.send(f"Checked **@{username}**.", ephemeral=True)


def main():
    if not TOKEN:
        raise SystemExit(
            "DISCORD_TOKEN is not set. Copy .env.example to .env and fill it in."
        )
    bot.run(TOKEN)


if __name__ == "__main__":
    main()
