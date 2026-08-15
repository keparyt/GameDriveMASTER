from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

# Put your SteamGridDB API key in this local config file.
# Do NOT commit a real key to a public repository.
STEAMGRIDDB_API_KEY = ""
STEAMGRIDDB_ENABLED = bool(STEAMGRIDDB_API_KEY)
STEAMGRIDDB_BASE_URL = "https://www.steamgriddb.com/api/v2"

# Artwork cache directory, relative to this project directory.
ARTWORK_CACHE_DIR = "data/images"
