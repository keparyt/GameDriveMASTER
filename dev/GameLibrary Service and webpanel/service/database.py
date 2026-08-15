from pathlib import Path
import sqlite3
import threading
from datetime import datetime, timezone

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "data" / "database.db"


def now():
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, path=DB_PATH):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(str(path), check_same_thread=False, timeout=30)
        self.conn.row_factory = sqlite3.Row
        with self.lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA foreign_keys=ON")
            self.conn.execute("PRAGMA busy_timeout=30000")
            self.init()

    def init(self):
        with self.lock:
            self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS drives (
                id INTEGER PRIMARY KEY,
                uuid TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL DEFAULT '',
                description TEXT NOT NULL DEFAULT '',
                last_letter TEXT,
                connected INTEGER NOT NULL DEFAULT 0,
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS games (
                id INTEGER PRIMARY KEY,
                drive_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                relative_path TEXT NOT NULL,
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                FOREIGN KEY(drive_id) REFERENCES drives(id) ON DELETE CASCADE,
                UNIQUE(drive_id, relative_path)
            );
            CREATE TABLE IF NOT EXISTS metadata (
                game_id INTEGER PRIMARY KEY,
                title TEXT, app_id TEXT, capsule TEXT, logo TEXT, hero TEXT, cover TEXT,
                release_date TEXT, description TEXT, updated_at TEXT NOT NULL,
                FOREIGN KEY(game_id) REFERENCES games(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_games_name ON games(name COLLATE NOCASE);
            CREATE INDEX IF NOT EXISTS idx_drives_uuid ON drives(uuid);
            CREATE INDEX IF NOT EXISTS idx_metadata_title ON metadata(title COLLATE NOCASE);
            """)
            self.conn.commit()

    def mark_disconnected(self):
        return 0

    def apply_drive_scan(self, uuid, name, description, letter, games, legacy_uuid=None):
        timestamp = now()
        with self.lock:
            row = self.conn.execute("SELECT id FROM drives WHERE uuid=?", (uuid,)).fetchone()
            if not row and legacy_uuid:
                row = self.conn.execute("SELECT id FROM drives WHERE uuid=?", (legacy_uuid,)).fetchone()
                if row:
                    self.conn.execute("UPDATE drives SET uuid=? WHERE id=?", (uuid, row["id"]))
            if row:
                drive_id = row["id"]
                self.conn.execute(
                    "UPDATE drives SET name=?,description=?,last_letter=?,connected=1,last_seen=? WHERE id=?",
                    (name, description, letter, timestamp, drive_id),
                )
            else:
                cursor = self.conn.execute(
                    "INSERT INTO drives (uuid,name,description,last_letter,connected,first_seen,last_seen) VALUES (?,?,?,?,1,?,?)",
                    (uuid, name, description, letter, timestamp, timestamp),
                )
                drive_id = cursor.lastrowid

            seen_paths = set()
            for game in games:
                relative_path = game["relative_path"]
                seen_paths.add(relative_path)
                self.conn.execute("""
                    INSERT INTO games (drive_id,name,relative_path,first_seen,last_seen)
                    VALUES (?,?,?,?,?)
                    ON CONFLICT(drive_id,relative_path) DO UPDATE SET
                        name=excluded.name,last_seen=excluded.last_seen
                """, (drive_id, game["name"], relative_path, timestamp, timestamp))

            rows = self.conn.execute("SELECT id,relative_path FROM games WHERE drive_id=?", (drive_id,)).fetchall()
            for game in rows:
                if game["relative_path"] not in seen_paths:
                    self.conn.execute("DELETE FROM games WHERE id=?", (game["id"],))
            self.conn.commit()
            return drive_id

    def apply_scan_connectivity(self, discovered_uuids, observed_letters):
        """Apply connectivity only from a completed scan.

        A known drive whose mount letter is still present but whose GameDrive scan
        failed is preserved. A drive is marked offline only when its previous
        mount is absent from the completed OS drive-discovery result.
        """
        discovered = set(discovered_uuids or ())
        observed = {str(letter).rstrip("\\").upper()[:1] for letter in (observed_letters or [])}
        if not observed:
            return 0
        timestamp = now()
        with self.lock:
            rows = self.conn.execute("SELECT id,uuid,last_letter,connected FROM drives").fetchall()
            changed = 0
            for row in rows:
                previous_letter = str(row["last_letter"] or "").upper()[:1]
                if row["uuid"] in discovered:
                    new_state = 1
                elif previous_letter and previous_letter not in observed:
                    new_state = 0
                else:
                    new_state = int(bool(row["connected"]))
                if int(bool(row["connected"])) != new_state:
                    changed += 1
                self.conn.execute(
                    "UPDATE drives SET connected=?,last_seen=CASE WHEN ?=1 THEN ? ELSE last_seen END WHERE id=?",
                    (new_state, new_state, timestamp, row["id"]),
                )
            self.conn.commit()
            return changed

    def upsert_drive(self, uuid, name, description, letter, legacy_uuid=None):
        return self.apply_drive_scan(uuid, name, description, letter, [], legacy_uuid=legacy_uuid)

    def upsert_game(self, drive_id, name, relative_path):
        timestamp = now()
        with self.lock:
            self.conn.execute("""
                INSERT INTO games (drive_id,name,relative_path,first_seen,last_seen)
                VALUES (?,?,?,?,?)
                ON CONFLICT(drive_id,relative_path) DO UPDATE SET name=excluded.name,last_seen=excluded.last_seen
            """, (drive_id, name, relative_path, timestamp, timestamp))
            self.conn.commit()

    def remove_missing_games(self, drive_id, seen_paths):
        with self.lock:
            rows = self.conn.execute("SELECT id,relative_path FROM games WHERE drive_id=?", (drive_id,)).fetchall()
            for row in rows:
                if row["relative_path"] not in seen_paths:
                    self.conn.execute("DELETE FROM games WHERE id=?", (row["id"],))
            self.conn.commit()

    def search(self, query="", connected_only=False):
        query = query.strip()
        params = []
        conditions = []
        if query:
            conditions.append("(g.name LIKE ? OR m.title LIKE ?)")
            value = f"%{query}%"
            params.extend([value, value])
        if connected_only:
            conditions.append("d.connected=1")
        where = "WHERE " + " AND ".join(conditions) if conditions else ""
        with self.lock:
            return self.conn.execute(f"""
                SELECT g.id,g.name,g.relative_path,d.uuid,d.name AS drive_name,d.description AS drive_description,
                       d.last_letter,d.connected,d.last_seen,m.title,m.app_id,m.capsule,m.logo,m.hero,m.cover,
                       m.release_date,m.description AS game_description
                FROM games g JOIN drives d ON d.id=g.drive_id LEFT JOIN metadata m ON m.game_id=g.id
                {where}
                ORDER BY d.connected DESC, COALESCE(m.title,g.name) COLLATE NOCASE
            """, params).fetchall()

    def get_game(self, game_id):
        with self.lock:
            return self.conn.execute("""
                SELECT g.*,d.uuid,d.name AS drive_name,d.description AS drive_description,d.last_letter,d.connected,d.last_seen,
                       m.title,m.app_id,m.capsule,m.logo,m.hero,m.cover,m.release_date,m.description AS game_description
                FROM games g JOIN drives d ON d.id=g.drive_id LEFT JOIN metadata m ON m.game_id=g.id WHERE g.id=?
            """, (game_id,)).fetchone()

    def set_metadata(self, data_game_id, data):
        with self.lock:
            game = self.conn.execute("SELECT id FROM games WHERE id=?", (data_game_id,)).fetchone()
            if not game:
                return False
            self.conn.execute("""
                INSERT INTO metadata (game_id,title,app_id,capsule,logo,hero,cover,release_date,description,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(game_id) DO UPDATE SET title=excluded.title,app_id=excluded.app_id,capsule=excluded.capsule,
                    logo=excluded.logo,hero=excluded.hero,cover=excluded.cover,release_date=excluded.release_date,
                    description=excluded.description,updated_at=excluded.updated_at
            """, (data_game_id,data.get("title"),data.get("app_id"),data.get("capsule"),data.get("logo"),data.get("hero"),data.get("cover"),data.get("release_date"),data.get("description"),now()))
            self.conn.commit()
            return True

    def update_metadata_fields(self, data_game_id, data):
        with self.lock:
            game = self.conn.execute("SELECT id FROM games WHERE id=?", (data_game_id,)).fetchone()
            if not game:
                return False
            current = self.conn.execute("SELECT * FROM metadata WHERE game_id=?", (data_game_id,)).fetchone()
            fields = ["title", "app_id", "capsule", "logo", "hero", "cover", "release_date", "description"]
            values = {field: (current[field] if current else None) for field in fields}
            for field in fields:
                if data.get(field) is not None:
                    values[field] = data[field]
            self.conn.execute("""
                INSERT INTO metadata (game_id,title,app_id,capsule,logo,hero,cover,release_date,description,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(game_id) DO UPDATE SET
                    title=excluded.title,app_id=excluded.app_id,capsule=excluded.capsule,logo=excluded.logo,
                    hero=excluded.hero,cover=excluded.cover,release_date=excluded.release_date,
                    description=excluded.description,updated_at=excluded.updated_at
            """, (data_game_id, values["title"], values["app_id"], values["capsule"], values["logo"], values["hero"], values["cover"], values["release_date"], values["description"], now()))
            self.conn.commit()
            return True

    def close(self):
        with self.lock:
            self.conn.close()
