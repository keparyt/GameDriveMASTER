import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

# Set locally or through the STEAMGRIDDB_API_KEY environment variable.
# Never commit a real API key to this public repository.
STEAMGRIDDB_API_KEY = os.environ.get("STEAMGRIDDB_API_KEY", "")
STEAMGRIDDB_ENABLED = bool(STEAMGRIDDB_API_KEY.strip())
STEAMGRIDDB_BASE_URL = "https://www.steamgriddb.com/api/v2"

# Artwork cache directory, relative to this project directory.
ARTWORK_CACHE_DIR = "data/images"
