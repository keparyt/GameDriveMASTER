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
    value = powershell_drive_value(drive, "$d.SerialNumber")
    return value.strip() if value else None


def partition_unique_id(drive):
    value = powershell_drive_value(drive, "$p.UniqueId")
    return value.strip() if value else None


def partition_number(drive):
    value = powershell_drive_value(drive, "$p.PartitionNumber")
    try:
        return int(value) if value else None
    except (TypeError, ValueError):
        return None


def drive_id(drive):
    partition_uid = partition_unique_id(drive)
    if partition_uid:
        return f"PARTITION:{partition_uid}"
    serial = physical_serial(drive)
    number = partition_number(drive)
    if serial and number is not None:
        return f"PARTITION:{serial}:{number}"
    volume = volume_serial(drive)
    if volume:
        return f"VOL:{volume}"
    return None


def legacy_drive_id(drive):
    serial = physical_serial(drive)
    return f"SERIAL:{serial}" if serial else None


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
        """Discover a drive completely without changing persistent state."""
        root = Path(drive)
        config_file = root / self.config["config_file"]
        if not config_file.is_file():
            return None
        info = read_ini(config_file)
        if not info:
            return None

        uid = drive_id(drive)
        if not uid:
            log.warning("Could not identify drive partition %s", drive)
            return None

        games_root = root / self.config["game_folder"]
        if not games_root.is_dir():
            log.warning("Game folder unavailable on %s; scan result is incomplete", drive)
            return None

        games = []
        try:
            for item in games_root.iterdir():
                if not item.is_dir() or not item.name.strip():
                    continue
                games.append({
                    "name": item.name.strip(),
                    "relative_path": item.relative_to(root).as_posix(),
                })
        except OSError as exc:
            log.warning("Cannot fully scan %s; preserving previous state: %s", games_root, exc)
            return None

        return {
            "uuid": uid,
            "legacy_uuid": legacy_drive_id(drive),
            "name": info["name"],
            "description": info["description"],
            "letter": drive[0],
            "games": games,
        }

    def scan(self):
        """Discover first, then atomically apply only complete results.

        Scanning is deliberately side-effect free until every successfully
        discovered GameDrive has a complete game-folder result. A failed,
        timed-out, or partial scan therefore cannot create transient offline
        states or replace the current library with partial data.
        """
        letters = drive_letters()
        if not letters:
            log.warning("Drive discovery unavailable; preserving last known state")
            return 0

        results = []
        for drive in letters:
            try:
                result = self.scan_drive(drive)
                if result:
                    results.append(result)
            except Exception:
                log.exception("Error scanning %s; preserving previous state", drive)

        if not results:
            log.warning("Scan produced no complete GameDrive results; preserving last known state")
            return 0

        discovered = set()
        for result in results:
            try:
                self.db.apply_drive_scan(
                    result["uuid"],
                    result["name"],
                    result["description"],
                    result["letter"],
                    result["games"],
                    legacy_uuid=result["legacy_uuid"],
                )
                discovered.add(result["uuid"])
            except Exception:
                log.exception("Could not commit completed scan for %s", result["letter"])

        # Only a completed discovery pass is authoritative for connectivity.
        # Drives not found in this completed pass are therefore genuinely absent.
        changed = self.db.apply_scan_connectivity(discovered)
        log.info(
            "Scan finished: %d complete GameDrive partition(s); connectivity updates=%d",
            len(discovered), changed,
        )
        return len(discovered)
