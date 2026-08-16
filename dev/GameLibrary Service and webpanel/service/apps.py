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
    """Resolve an app entrypoint from exepath.txt.

    Entries may be executables or small launcher scripts. Script files are
    converted to an interpreter command so the normal app launch endpoint
    can start them through subprocess.Popen.
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
    candidates = [p] if p.is_absolute() else [game_dir / p, app_folder / p]
    app_root = app_folder.resolve()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
            if not ((app_root == resolved or app_root in resolved.parents) and resolved.is_file()):
                continue

            suffix = resolved.suffix.lower()
            extra = tokens[1:]
            system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))

            if suffix == ".ps1":
                return system_root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe", [
                    "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(resolved), *extra
                ]
            if suffix in (".cmd", ".bat"):
                return Path(os.environ.get("ComSpec", "cmd.exe")), [
                    "/d", "/c", str(resolved), *extra
                ]
            if suffix == ".py":
                return Path(os.environ.get("PYTHON", "python.exe")), [str(resolved), *extra]
            if suffix == ".vbs":
                return system_root / "System32" / "wscript.exe", [str(resolved), *extra]

            return resolved, extra
        except (OSError, ValueError):
            continue
    return None, []


def _find_image(folder, names):
    wanted = {n.lower() for n in names}
    try:
        for path in folder.iterdir():
            if path.is_file() and path.name.lower() in wanted:
                return str(path.resolve())
    except OSError:
        pass
    return None


def scan_apps(root=None):
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
        cover_file = _find_image(folder, ("Capsule.jpg", "Capsule.jpeg", "Capsule.png", "Capsule.webp"))
        hero_file = _find_image(folder, ("Hero.jpg", "Hero.jpeg", "Hero.png", "Hero.webp", "Background.jpg", "Background.jpeg", "Background.png", "Background.webp"))
        icon_file = _find_image(folder, ("Icon.png", "Icon.jpg", "Icon.jpeg", "Icon.webp", "Capsule.png", "Capsule.jpg", "Capsule.jpeg", "Capsule.webp"))
        # Return browser-safe API URLs rather than Windows filesystem paths.
        # This makes both cards and detail heroes work on the web panel.
        media = lambda kind, present: f"/api/apps/media/{__import__('urllib.parse', fromlist=['quote']).quote(folder.name, safe='')}/{kind}" if present else None
        cover = media("cover", cover_file)
        capsule = media("capsule", cover_file)
        hero = media("hero", hero_file or cover_file)
        icon = media("icon", icon_file)
        logo = media("logo", icon_file)
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
            "capsule": capsule,
            "hero": hero,
            "icon": icon,
            "logo": logo,
            "has_cover": bool(cover_file),
            "has_hero": bool(hero_file),
        })
    return result


def find_app(app_id, root=None):
    target = str(app_id or "").casefold()
    return next((x for x in scan_apps(root) if x["app_id"].casefold() == target), None)
