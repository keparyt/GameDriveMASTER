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


def physical_uuid(drive):
    # Disk UniqueId is preferred because it follows the physical disk when its
    # drive letter changes. SerialNumber is only a fallback for older devices.
    return powershell_drive_value(drive, "$d.UniqueId")


def physical_serial(drive):
    return powershell_drive_value(drive, "$d.SerialNumber")


def drive_id(drive):
    unique_id = physical_uuid(drive)
    if unique_id:
        return f"UUID:{unique_id}"
    serial = physical_serial(drive)
    volume = volume_serial(drive)
    if serial and volume:
        return f"DISK:{serial}|VOL:{volume}"
    if serial:
        return f"DISK:{serial}"
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

        log.debug("Drive identity: letter=%s uuid=%s name=%r", drive[0], uid, info["name"])
        drive_id_value = self.db.upsert_drive(uid, info["name"], info["description"], drive[0])
        games_root = root / self.config["game_folder"]
        if not games_root.is_dir():
            self.db.remove_missing_games(drive_id_value, set())
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
            log.warning("Cannot scan %s: %s", games_root, exc)

        self.db.remove_missing_games(drive_id_value, seen_paths)
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
