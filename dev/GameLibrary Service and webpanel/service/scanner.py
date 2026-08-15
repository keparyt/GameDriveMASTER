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
    """Evaluate an expression against both the partition ($p) and disk ($d)."""
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


def partition_unique_id(drive):
    """Return Windows' stable unique ID for this partition."""
    value = powershell_drive_value(drive, "$p.UniqueId")
    return value.strip() if value else None


def partition_number(drive):
    value = powershell_drive_value(drive, "$p.PartitionNumber")
    try:
        return int(value) if value else None
    except (TypeError, ValueError):
        return None


def drive_id(drive):
    """Return a partition identity, not just a physical-disk identity.

    A 2 TB HDD can contain several drive letters/partitions. Using only the
    physical disk serial would make all of those partitions overwrite the same
    database row. Prefer the partition UniqueId, then fall back to the physical
    serial + partition number, then the volume serial.
    """
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
    """Return the pre-partition-aware identity for database migration."""
    serial = physical_serial(drive)
    if serial:
        return f"SERIAL:{serial}"
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
        """Inspect one logical drive without inferring offline from partial data."""
        root = Path(drive)
        config_file = root / self.config["config_file"]

        # Missing/inaccessible config is not proof that the drive is offline.
        # The drive may be busy, partially mounted, or temporarily inaccessible.
        if not config_file.is_file():
            log.debug("GameDrive config unavailable on %s; preserving previous state", drive)
            return None

        info = read_ini(config_file)
        if not info:
            return None

        uid = drive_id(drive)
        if not uid:
            log.warning("Could not identify drive partition %s; preserving previous state", drive)
            return None

        legacy_uid = legacy_drive_id(drive)
        log.debug("Partition identity: letter=%s identity=%s", drive[0], uid)
        drive_id_value = self.db.upsert_drive(
            uid,
            info["name"],
            info["description"],
            drive[0],
            legacy_uuid=legacy_uid,
        )

        games_root = root / self.config["game_folder"]
        if not games_root.is_dir():
            log.warning("Game folder unavailable on %s; drive is online but game scan is incomplete", drive)
            return {"drive_id": drive_id_value, "complete": False, "seen_paths": set()}

        # Build the entire result in memory first. Missing games are only
        # committed after iteration finishes without an OSError.
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
            log.warning("Cannot fully scan %s; preserving existing games: %s", games_root, exc)
            return {"drive_id": drive_id_value, "complete": False, "seen_paths": set()}

        self.db.apply_complete_scan(drive_id_value, games)
        log.debug("Complete scan indexed %d game folders on %s", len(games), drive)
        return {
            "drive_id": drive_id_value,
            "uuid": uid,
            "complete": True,
            "seen_paths": {item["relative_path"] for item in games},
        }

    def scan(self):
        """Run a scan without resetting all known drives to OFFLINE first.

        A drive becomes OFFLINE only when its previously known logical drive
        letter is absent from the OS drive set and the drive was not positively
        rediscovered under another letter during this scan. Scan errors,
        missing folders, and incomplete results preserve the previous state.
        """
        # On non-Windows there is no authoritative logical-drive signal, so a
        # scan must never mass-mark the database offline.
        if not hasattr(ctypes, "windll"):
            log.debug("Logical drive detection unavailable; skipping availability transitions")
            return 0

        drives = drive_letters()
        drive_set = {drive.upper() for drive in drives}
        known = self.db.get_drives()
        rediscovered_ids = set()
        found = 0

        for drive in drives:
            try:
                result = self.scan_drive(drive)
                if result:
                    found += 1
                    if result.get("uuid"):
                        rediscovered_ids.add(result["uuid"])
            except Exception:
                # A scanner exception is not reliable evidence of a disconnect.
                log.exception("Error scanning %s; preserving previous availability state", drive)

        # The only offline transition here is based on explicit OS evidence:
        # the known drive letter is no longer present. If the same partition
        # was found under a different letter, it remains online.
        for row in known:
            letter = row["last_letter"]
            if not letter or str(letter).upper() in drive_set:
                continue
            if row["uuid"] in rediscovered_ids:
                continue
            if self.db.mark_drive_offline(row["id"]):
                log.info("Confirmed GameDrive offline: %s (%s:)", row["name"], letter)

        log.info("Scan finished: %d GameDrive partition(s) online", found)
        return found
