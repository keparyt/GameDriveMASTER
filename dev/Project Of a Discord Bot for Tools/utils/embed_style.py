"""Shared Discord embed styling for the bot's user-facing panels."""

from datetime import datetime, timezone

import discord

# One restrained palette keeps the bot UI visually consistent without making
# every panel look identical.
PRIMARY = discord.Color.from_rgb(39, 43, 48)
SUCCESS = discord.Color.from_rgb(46, 125, 85)
WARNING = discord.Color.from_rgb(190, 135, 45)
DANGER = discord.Color.from_rgb(175, 65, 65)
INFO = discord.Color.from_rgb(65, 105, 145)

BRAND_FOOTER = "Kepar Lab Assist"


def panel(
    title: str,
    description: str | None = None,
    *,
    color: discord.Color = PRIMARY,
    footer: str | None = None,
    timestamp: bool = False,
) -> discord.Embed:
    embed = discord.Embed(
        title=title,
        description=description,
        color=color,
    )
    embed.set_footer(text=footer or BRAND_FOOTER)
    if timestamp:
        embed.timestamp = datetime.now(timezone.utc)
    return embed


def status(title: str, description: str, *, footer: str | None = None) -> discord.Embed:
    return panel(title, description, color=INFO, footer=footer)


def success(title: str, description: str, *, footer: str | None = None) -> discord.Embed:
    return panel(title, description, color=SUCCESS, footer=footer)


def warning(title: str, description: str, *, footer: str | None = None) -> discord.Embed:
    return panel(title, description, color=WARNING, footer=footer)


def error(title: str, description: str, *, footer: str | None = None) -> discord.Embed:
    return panel(title, description, color=DANGER, footer=footer)
