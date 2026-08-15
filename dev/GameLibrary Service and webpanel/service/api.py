from pathlib import Path

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse


BASE_DIR = Path(__file__).resolve().parent.parent
WEB_DIR = BASE_DIR / "web"
ARTWORK_DIR = BASE_DIR / "data" / "images"


def create_app(db, metadata=None):
    app = FastAPI(title="Game Library API", version="1.0.0")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://127.0.0.1", "http://localhost"],
        allow_methods=["GET"],
        allow_headers=["*"]
    )

    @app.get("/", response_class=HTMLResponse)
    def index():
        return (WEB_DIR / "index.html").read_text(encoding="utf-8")

    @app.get("/style.css")
    def css():
        return FileResponse(WEB_DIR / "style.css", media_type="text/css")

    @app.get("/app.js")
    def javascript():
        return FileResponse(WEB_DIR / "app.js", media_type="application/javascript")

    @app.get("/artwork/{game_name}/{filename}")
    def artwork(game_name: str, filename: str):
        path = (ARTWORK_DIR / game_name / filename).resolve()
        root = ARTWORK_DIR.resolve()
        if root not in path.parents or not path.is_file():
            return {"error": "not_found"}
        return FileResponse(path)

    @app.get("/api/health")
    def health():
        return {
            "ok": True,
            "service": "GameLibrary",
            "metadata_enabled": bool(metadata and metadata.enabled),
            "artwork_queue": metadata._queue.qsize() if metadata else 0
        }

    @app.get("/api/games")
    def games(q: str = Query("", max_length=200), connected_only: bool = False):
        rows = db.search(q, connected_only)
        if metadata and metadata.auto_lookup:
            for row in rows:
                if not row["capsule"]:
                    metadata.queue_lookup(row["id"], row["name"])
        return [dict(row) for row in db.search(q, connected_only)]

    @app.get("/api/games/{game_id}")
    def game(game_id: int):
        row = db.get_game(game_id)
        if not row:
            return {"error": "not_found"}
        if metadata and not row["capsule"]:
            metadata.queue_lookup(game_id, row["name"])
        return dict(db.get_game(game_id))

    return app
