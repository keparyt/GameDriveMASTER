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


def _resolve_app_executable(app_folder, game_dir, text):
    """Resolve an app executable from exepath.txt.

    Accepts a relative path (normally relative to Game/) or an absolute path
    when it points inside the app folder. The latter is useful for existing
    app definitions that already contain a fully-qualified Windows path.
    """
    text = (text or "").strip()
    if not text:
        return None, []
    try:
        tokens = shlex.split(text, posix=False)
    except ValueError:
        tokens = [text]
    if not tokens:
        return None, []

    raw = tokens[0].strip().strip('"').strip("'")
    if not raw:
        return None, []

    p = Path(raw)
    candidates = []
    if p.is_absolute():
        candidates.append(p)
    else:
        # Game/<path> is the documented layout, while app/<path> keeps
        # compatibility with older app packages.
        candidates.extend((game_dir / p, app_folder / p))

    app_root = app_folder.resolve()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
            if (app_root == resolved or app_root in resolved.parents) and resolved.is_file():
                return resolved, tokens[1:]
        except (OSError, ValueError):
            continue
    return None, []


def _find_image(folder, names):
    for name in names:
        path = folder / name
        if path.is_file():
            return str(path.resolve())
    # Also accept case variations on Windows/filesystems where the exact
    # spelling differs.
    wanted = {n.lower() for n in names}
    try:
        for path in folder.iterdir():
            if path.is_file() and path.name.lower() in wanted:
                return str(path.resolve())
    except OSError:
        pass
    return None


def scan_apps(root=None):
    """Scan every apps/<name> folder and return launcher-ready entries."""
    root = Path(root or os.environ.get("GAMEDRIVE_APPS_PATH") or APPS_DIR)
    if not root.is_dir():
        return []

    result = []
    for folder in sorted(root.iterdir(), key=lambda p: p.name.lower()):
        if not folder.is_dir():
            continue
        exepath = folder / "exepath.txt"
        game_dir = folder / "Game"
        if not exepath.is_file():
            continue

        executable, arguments = _resolve_app_executable(folder, game_dir, _read_text(exepath))
        if not executable:
            # Keep the entry visible so a bad package cannot silently vanish
            # from the Apps library. It remains clearly marked unlaunchable.
            arguments = []

        cover = _find_image(folder, ("Capsule.jpg", "Capsule.jpeg", "Capsule.png"))
        icon = _find_image(folder, ("Icon.png", "Icon.jpg", "Icon.jpeg", "Capsule.png", "Capsule.jpg", "Capsule.jpeg"))
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
            "install_directory": str(game_dir.resolve()) if game_dir.is_dir() else str(folder.resolve()),
            "executable": str(executable) if executable else None,
            "launchable": bool(executable),
            "arguments": arguments,
            "working_directory": str(game_dir.resolve()) if game_dir.is_dir() else str(folder.resolve()),
            "relative_path": str(folder.relative_to(root)),
            "cover": cover,
            "capsule": cover,
            "icon": icon,
            "logo": icon,
        })
    return result


def find_app(app_id, root=None):
    target = str(app_id or "").casefold()
    return next((x for x in scan_apps(root) if x["app_id"].casefold() == target), None)
