import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sys

SERVICE_DIR = Path(__file__).resolve().parents[1]
if str(SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(SERVICE_DIR))

from service import scanner as scanner_module
from service.database import Database
from service.scanner import Scanner


class AvailabilityTests(unittest.TestCase):
    def test_complete_scan_controls_game_availability(self):
        with tempfile.TemporaryDirectory() as temp:
            db = Database(Path(temp) / "database.db")
            drive_id = db.upsert_drive("drive-1", "Games", "", "E")

            db.apply_complete_scan(drive_id, [
                {"name": "Game A", "relative_path": "Games/Game A"},
                {"name": "Game B", "relative_path": "Games/Game B"},
            ])
            self.assertEqual({row["name"] for row in db.search(connected_only=True)}, {"Game A", "Game B"})

            # A positive but incomplete refresh does not infer that Game B vanished.
            db.upsert_drive("drive-1", "Games", "", "E")
            self.assertEqual({row["name"] for row in db.search(connected_only=True)}, {"Game A", "Game B"})

            # Only a complete scan is authoritative for removals.
            db.apply_complete_scan(drive_id, [
                {"name": "Game A", "relative_path": "Games/Game A"},
            ])
            connected = db.search(connected_only=True)
            self.assertEqual([row["name"] for row in connected], ["Game A"])
            all_games = {row["name"]: row["connected"] for row in db.search()}
            self.assertEqual(all_games["Game B"], 0)
            db.close()

    def test_scan_does_not_mark_present_drive_offline(self):
        class FakeDb:
            def get_drives(self):
                return [{"id": 1, "uuid": "drive-1", "name": "Games", "last_letter": "E", "connected": 1}]

            def mark_drive_offline(self, drive_id):
                raise AssertionError("present drive must not be marked offline")

        scanner = Scanner(FakeDb(), {})
        with patch.object(scanner_module.ctypes, "windll", object()), \
             patch("service.scanner.drive_letters", return_value=["E:\\"]), \
             patch.object(scanner, "scan_drive", return_value=None):
            scanner.scan()

    def test_scan_marks_missing_drive_offline(self):
        calls = []

        class FakeDb:
            def get_drives(self):
                return [{"id": 1, "uuid": "drive-1", "name": "Games", "last_letter": "E", "connected": 1}]

            def mark_drive_offline(self, drive_id):
                calls.append(drive_id)
                return True

        scanner = Scanner(FakeDb(), {})
        with patch.object(scanner_module.ctypes, "windll", object()), \
             patch("service.scanner.drive_letters", return_value=[]):
            scanner.scan()
        self.assertEqual(calls, [1])

    def test_scan_preserves_drive_when_rediscovered_under_new_letter(self):
        class FakeDb:
            def get_drives(self):
                return [{"id": 1, "uuid": "drive-1", "name": "Games", "last_letter": "E", "connected": 1}]

            def mark_drive_offline(self, drive_id):
                raise AssertionError("rediscovered drive must remain online")

        scanner = Scanner(FakeDb(), {})
        with patch.object(scanner_module.ctypes, "windll", object()), \
             patch("service.scanner.drive_letters", return_value=["F:\\"]), \
             patch.object(scanner, "scan_drive", return_value={"uuid": "drive-1"}):
            scanner.scan()


if __name__ == "__main__":
    unittest.main()
