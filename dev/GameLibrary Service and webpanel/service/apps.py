from pathlib import Path
import os
import shlex

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


def _resolve_app_executable(game_dir, text):
    """Resolve exepath.txt safely while accepting a relative path or quoted path."""
    text = (text or "").strip().strip('"')
    if not text:
        return None, []

    # Optional command-line arguments are supported. The first token is the executable.
    try:
        tokens = shlex.split(text, posix=False)
    except ValueError:
        tokens = [text]
    if not tokens:
        return None, []

    rel = tokens[0].strip().strip('"').replace("/", "\\")
    p = Path(rel)
    if p.is_absolute() or ".." in p.parts:
        return None, []

    try:
        game_root = game_dir.resolve()
        executable = (game_root / rel).resolve()
        if game_root not in executable.parents or not executable.is_file():
            return None, []
    except OSError:
        return None, []

    return executable, tokens[1:]


def _find_image(folder, names):
    for name in names:
        path = folder / name
        if path.is_file():
            return str(path.resolve())
    return None


def scan_apps(root=None):
    """Scan apps/<name> using exepath.txt + Game and return launcher-ready entries."""
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

        executable, arguments = _resolve_app_executable(game_dir, _read_text(exepath))
        if not executable:
            continue

        cover = _find_image(folder, ("Capsule.jpg", "Capsule.jpeg", "Capsule.png"))
        icon = _find_image(folder, ("Icon.png", "Icon.jpg", "Icon.jpeg", "Capsule.png", "Capsule.jpg"))
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
            "arguments": arguments,
            "working_directory": str(game_dir.resolve()),
            "relative_path": str(folder.relative_to(root)),
            "cover": cover,
            "capsule": cover,
            "icon": icon,
            "logo": icon,
        })
    return result


def find_app(app_id, root=None):
    return next((x for x in scan_apps(root) if x["app_id"] == app_id), None)
