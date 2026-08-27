import discord

from processors.candidate_filter import confidence_percent
from utils.embed_style import SUCCESS, WARNING, panel, warning


def _normalize_game_name(value: str) -> str:
    import re

    value = re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold())
    return re.sub(r"\s+", " ", value).strip()


def _confidence_text(value) -> str:
    if value is None:
        return "verified"
    return f"{confidence_percent(value)}%"


def create_result_embed(
    result: dict,
    installed_names: set[str],
    captured_input: str | None = None,
) -> discord.Embed:
    """Render only user-useful detection information.

    Internal candidate dictionaries, rejection reasons, source flags and raw
    OCR are deliberately excluded from this UI. Those belong in backend logs.
    """
    games = result.get("games") or []
    unresolved = result.get("unresolved_games") or []

    if not games:
        embed = warning(
            "🎮 No verified games found",
            "No game title could be identified with enough confidence to add to the download queue.",
            footer="Game analysis • unverified candidates were excluded",
        )
        if unresolved:
            embed.add_field(
                name="⚠️ Unresolved",
                value="Some detected text could not be confidently identified as a game and was excluded.",
                inline=False,
            )
        return embed

    embed = panel(
        "🎮 Game Detection Results",
        f"Found **{len(games)}** verified game{'s' if len(games) != 1 else ''}. Select the games you want to add to the download queue.",
        color=SUCCESS,
        footer="Private game analysis • only verified games are selectable",
    )

    lines = []
    for index, game in enumerate(games[:25], start=1):
        name = str(game.get("name") or "Unknown game").strip()
        score = _confidence_text(game.get("confidence"))
        installed = _normalize_game_name(name) in installed_names
        status = " · ✅ Already installed" if installed else ""
        lines.append(f"**{index}. {name}** · `{score}`{status}")

    embed.add_field(
        name="✅ Verified Games",
        value="\n".join(lines)[:1024],
        inline=False,
    )

    if unresolved:
        embed.add_field(
            name="⚠️ Unresolved",
            value=f"**{len(unresolved)}** possible match{'es' if len(unresolved) != 1 else ''} ignored because they could not be confidently verified.",
            inline=False,
        )

    return embed
