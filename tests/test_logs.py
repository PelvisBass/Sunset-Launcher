"""Exercise rotation of launcher-owned logs in temporary directories."""

import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from sunset_launcher.logs import open_run_log, prune_run_logs


class LogTests(unittest.TestCase):
    @staticmethod
    def write_log(directory: Path, suffix: int, modified: int) -> Path:
        path = directory / f"20261123-140000-{suffix:06x}.log"
        path.write_text(f"Job {suffix}\n", encoding="utf-8")
        os.utime(path, (modified, modified))
        return path

    def test_startup_keeps_two_most_recent_launcher_logs(self):
        with TemporaryDirectory() as folder:
            directory = Path(folder)
            most_recent = self.write_log(directory, 1, 300)
            oldest = self.write_log(directory, 2, 100)
            previous = self.write_log(directory, 3, 200)
            manual = directory / "manual.txt"
            manual.write_text("Notes", encoding="utf-8")

            prune_run_logs(directory)

            self.assertFalse(oldest.exists())
            self.assertEqual(set(directory.iterdir()), {manual, most_recent, previous})

    def test_new_run_replaces_the_older_log(self):
        with TemporaryDirectory() as folder:
            directory = Path(folder)
            oldest = self.write_log(directory, 1, 100)
            previous = self.write_log(directory, 2, 200)

            with open_run_log(directory) as (current, stream):
                prune_run_logs(directory, current=current)
                stream.write("Current run\n")

            self.assertFalse(oldest.exists())
            self.assertEqual(set(directory.iterdir()), {current, previous})
            self.assertEqual(current.read_text(encoding="utf-8"), "Current run\n")

    def test_rotation_does_not_follow_linked_logs_or_log_directory(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            logs = root / "logs"
            logs.mkdir()
            external = root / "notes.log"
            external.write_text("Keep this", encoding="utf-8")
            linked_file = logs / "20261123-140000-00dead.log"
            linked_directory = root / "linked-logs"
            try:
                linked_file.symlink_to(external)
                linked_directory.symlink_to(logs, target_is_directory=True)
            except OSError as error:
                self.skipTest(f"Symlinks are unavailable: {error}")
            for index in range(3):
                self.write_log(logs, index, 100 + index)

            with self.assertRaises(OSError):
                prune_run_logs(linked_directory)
            with self.assertRaises(OSError), open_run_log(linked_directory):
                pass
            prune_run_logs(logs)

            self.assertTrue(linked_file.is_symlink())
            self.assertEqual(external.read_text(encoding="utf-8"), "Keep this")
            self.assertEqual(len(list(logs.iterdir())), 3)
