import discord

from utils.embed_style import SUCCESS, WARNING, panel, warning


def _normalize_game_name(value: str) -> str:
    import re

    value = re.sub(r"[^a-z0-9]+", " ", value.casefold())
    return re.sub(r"\s+", " ", value).strip()


def create_result_embed(
    result: dict,
    installed_names: set[str],
    captured_input: str | None = None,
) -> discord.Embed:
    """Build the user-facing game detection result embed.

    Kept outside the Discord event cog so both message-based detection and
    slash-command analysis share exactly one result-rendering implementation.
    """
    games = result.get("games") or []
    unresolved = result.get("unresolved_games") or []

    if not games and result.get("status") not in {"partial", "identified"}:
        embed = warning(
            "🎮 Game not identified",
            result.get("message", "I couldn't identify a supported game."),
            footer="Game analysis • no sufficiently strong match",
        )
        if captured_input:
            embed.add_field(
                name="Original input",
                value=f"```{captured_input[:900]}```",
                inline=False,
            )
        embed.add_field(
            name="Accuracy note",
            value=(
                "Only sufficiently strong title/platform matches are shown. "
                "Unresolved candidates are excluded from the queue."
            ),
            inline=False,
        )
        return embed

    title = "🎮 Games Identified" if not unresolved else "🎮 Games Identified — Some Unresolved"
    description = (
        "Found **%d** verified game(s). Select which game(s) should be sent "
        "to the massive library download queue."
        % len(games)
    )
    if unresolved:
        description += (
            "\n\n⚠️ **Not verified and excluded:** "
            + ", ".join(str(x) for x in unresolved[:12])
        )

    embed = panel(
        title,
        description,
        color=SUCCESS if games else WARNING,
        footer="Private game analysis • select only the games you want queued",
    )

    if captured_input:
        embed.add_field(
            name="Original input",
            value=f"```{captured_input[:900]}```",
            inline=False,
        )

    for index, game in enumerate(games[:25], start=1):
        name = str(game.get("name", "Unknown game"))
        score = game.get("confidence")
        score_text = (
            f"{float(score) * 100:.0f}%"
            if isinstance(score, (int, float))
            else "verified"
        )
        platform = str(
            game.get("selected_platform")
            or ("PC" if game.get("pc_available") else "Console")
        )
        consoles = game.get("console_platforms") or game.get("console_names") or []
        if isinstance(consoles, str):
            consoles = [consoles]
        consoles = ", ".join(
            dict.fromkeys(
                str(x).strip() for x in consoles if str(x).strip()
            )
        )

        details = f"— **{score_text}** · `{platform}`"
        if consoles:
            details += f" → `{consoles}`"

        evidence = game.get("evidence") or game.get("reason")
        if evidence:
            details += f"\n{str(evidence)[:180]}"

        if _normalize_game_name(name) in installed_names:
            details += "\n✅ Already installed"

        embed.add_field(
            name=f"{index}. {name}",
            value=details[:1024],
            inline=False,
        )

    return embed
