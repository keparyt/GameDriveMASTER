import logging


log = logging.getLogger("gamelibrary.metadata")


class MetadataManager:
    """
    Metadata abstraction.

    The database and scanner never need to know where metadata comes from.

    Later providers can be plugged in here, for example:
        SteamDB
        Steam
        IGDB
        another permitted API

    Returned metadata should use this structure:

    {
        "title": "...",
        "app_id": "...",
        "capsule": "...",
        "logo": "...",
        "hero": "...",
        "cover": "...",
        "release_date": "...",
        "description": "..."
    }
    """

    def __init__(self, db, config=None):
        self.db = db
        self.config = config or {}

    def lookup(self, game_id, game_name):
        row = self.db.get_game(game_id)

        if row and row["title"]:
            return dict(row)

        log.debug(
            "No metadata provider configured for %s",
            game_name
        )

        return None
