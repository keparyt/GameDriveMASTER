import configparser
import ctypes
import logging
import string
import subprocess
from pathlib import Path

log = logging.getLogger("gamelibrary.scanner")


def drive_letters():
    if not hasattr(ctypes, "windll"):
        return []
    mask = ctypes.windll.kernel32.GetLogicalDrives()
    return [f"{letter}:\\" for index, letter in enumerate(string.ascii_uppercase) if mask & (1 << index)]


def volume_serial(drive):
    if not hasattr(ctypes, "windll"):
        return None
    name = ctypes.create_unicode_buffer(261)
    filesystem = ctypes.create_unicode_buffer(261)
    serial = ctypes.c_ulong()
    max_component = ctypes.c_ulong()
    flags = ctypes.c_ulong()
    try:
        ok = ctypes.windll.kernel32.GetVolumeInformationW(
            ctypes.c_wchar_p(drive), name, len(name), ctypes.byref(serial),
            ctypes.byref(max_component), ctypes.byref(flags), filesystem, len(filesystem)
        )
        return f"{serial.value:08X}" if ok else None
    except Exception:
        return None


def powershell_drive_value(drive, expression):
    letter = drive[0]
    command = (
        f"$p=Get-Partition -DriveLetter '{letter}' -ErrorAction SilentlyContinue; "
        f"if($p){{ $d=Get-Disk -Number $p.DiskNumber -ErrorAction SilentlyContinue; "
        f"if($d){{ {expression} }} }}"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True, text=True, timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )
        value = result.stdout.strip()
        return value or None
    except Exception as exc:
        log.debug("PowerShell drive identity lookup failed for %s: %s", drive, exc)
        return None


def physical_serial(drive):
    """Return the manufacturer's physical disk serial number."""
    value = powershell_drive_value(drive, "$d.SerialNumber")
    return value.strip() if value else None


def physical_uuid(drive):
    return powershell_drive_value(drive, "$d.UniqueId")


def drive_id(drive):
    # Physical serial is the primary identity. This is the value normally
    # printed on the physical HDD/SSD label and survives drive-letter changes.
    serial = physical_serial(drive)
    if serial:
        return f"SERIAL:{serial}"

    unique_id = physical_uuid(drive)
    if unique_id:
        return f"UUID:{unique_id}"
    volume = volume_serial(drive)
    if volume:
        return f"VOL:{volume}"
    return None


def read_ini(path):
    config = configparser.ConfigParser(interpolation=None)
    try:
        config.read(path, encoding="utf-8-sig")
        if not config.has_section("Drive"):
            log.warning("%s does not contain [Drive]", path)
            return None
        return {
            "name": config.get("Drive", "name", fallback="").strip(),
            "description": config.get("Drive", "description", fallback="").strip()
        }
    except Exception as exc:
        log.warning("Cannot read %s: %s", path, exc)
        return None


class Scanner:
    def __init__(self, db, config):
        self.db = db
        self.config = config

    def scan_drive(self, drive):
        root = Path(drive)
        config_file = root / self.config["config_file"]
        if not config_file.is_file():
            return False
        info = read_ini(config_file)
        if not info:
            return False

        uid = drive_id(drive)
        if not uid:
            log.warning("Could not identify drive %s", drive)
            return False

        log.debug("Drive identity: letter=%s identity=%s", drive[0], uid)
        drive_id_value = self.db.upsert_drive(uid, info["name"], info["description"], drive[0])
        games_root = root / self.config["game_folder"]
        if not games_root.is_dir():
            log.warning("Game folder unavailable on %s; keeping existing indexed games", drive)
            return True

        seen_paths = set()
        try:
            for item in games_root.iterdir():
                if not item.is_dir() or not item.name.strip():
                    continue
                relative_path = item.relative_to(root).as_posix()
                seen_paths.add(relative_path)
                self.db.upsert_game(drive_id_value, item.name.strip(), relative_path)
        except OSError as exc:
            # Never remove anything when a scan cannot be completed. This is
            # especially important for large/multi-drive libraries where a
            # temporary filesystem error must not make games disappear.
            log.warning("Cannot fully scan %s; keeping existing games: %s", games_root, exc)
            return True

        # Do NOT delete missing games here. The library is persistent: a game
        # disappearing from a scan can be a transient filesystem/drive issue,
        # and metadata workers may still be processing that game. Keeping the
        # record also means disconnected drives retain their complete library.
        log.debug("Scan indexed %d game folders on %s; existing library entries preserved", len(seen_paths), drive)
        return True

    def scan(self):
        self.db.mark_disconnected()
        found = 0
        for drive in drive_letters():
            try:
                if self.scan_drive(drive):
                    found += 1
            except Exception:
                log.exception("Error scanning %s", drive)
        log.info("Scan finished: %d GameDrive(s) online", found)
        return found
