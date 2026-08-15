from pathlib import Path

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse


BASE_DIR = Path(__file__).resolve().parent.parent
WEB_DIR = BASE_DIR / "web"


def create_app(db):

    app = FastAPI(
        title="Game Library API",
        version="1.0.0"
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://127.0.0.1",
            "http://localhost"
        ],
        allow_methods=["GET"],
        allow_headers=["*"]
    )

    @app.get("/", response_class=HTMLResponse)
    def index():
        path = WEB_DIR / "index.html"

        return path.read_text(
            encoding="utf-8"
        )

    @app.get("/style.css")
    def css():
        return FileResponse(
            WEB_DIR / "style.css",
            media_type="text/css"
        )

    @app.get("/app.js")
    def javascript():
        return FileResponse(
            WEB_DIR / "app.js",
            media_type="application/javascript"
        )

    @app.get("/api/health")
    def health():
        return {
            "ok": True,
            "service": "GameLibrary"
        }

    @app.get("/api/games")
    def games(
        q: str = Query(
            "",
            max_length=200
        ),
        connected_only: bool = False
    ):
        return [
            dict(row)
            for row in db.search(
                q,
                connected_only
            )
        ]

    @app.get("/api/games/{game_id}")
    def game(game_id: int):
        row = db.get_game(game_id)

        if not row:
            return {
                "error": "not_found"
            }

        return dict(row)

    return app
