from pathlib import Path
import os

BASE_DIR = Path(__file__).resolve().parent.parent
APPS_DIR = BASE_DIR / "apps"


def _read_text(path):
    try:
        return path.read_text(encoding="utf-8-sig").strip()
    except Exception:
        try:
            return path.read_text(errors="ignore").strip()
        except Exception:
            return ""


def scan_apps(root=None):
    """Scan apps/<name> using the same exepath.txt + Game layout as GameDrive games."""
    root = Path(root or os.environ.get("GAMEDRIVE_APPS_PATH") or APPS_DIR)
    if not root.is_dir():
        return []
    result = []
    for folder in sorted(root.iterdir(), key=lambda p: p.name.lower()):
        if not folder.is_dir():
            continue
        exepath = folder / "exepath.txt"
        game_dir = folder / "Game"
        if not exepath.is_file() or not game_dir.is_dir():
            continue
        rel = _read_text(exepath).replace("/", "\\")
        if not rel or Path(rel).is_absolute() or ".." in Path(rel).parts:
            continue
        executable = (game_dir / rel).resolve()
        try:
            if game_dir.resolve() not in executable.parents or not executable.is_file():
                continue
        except OSError:
            continue
        result.append({
            "app_id": folder.name,
            "name": folder.name,
            "title": folder.name,
            "source": "apps",
            "category": "Apps",
            "unified_id": "app:" + folder.name,
            "connected": True,
            "installation_state": "Installed",
            "launch_source": "GameDrive Apps",
            "install_directory": str(game_dir.resolve()),
            "executable": str(executable),
            "working_directory": str(game_dir.resolve()),
            "relative_path": str(folder.relative_to(root)),
        })
    return result


def find_app(app_id, root=None):
    return next((x for x in scan_apps(root) if x["app_id"] == app_id), None)
