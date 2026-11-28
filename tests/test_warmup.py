"""Exercise shader warm-up estimates, memory readings, and the Install and Warm-up pages."""

import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication

from sunset_launcher.__main__ import apply_theme
from sunset_launcher.backend import Library, OperationError, WarmupProgress, recipe_for
from sunset_launcher.catalog import PROJECTS
from sunset_launcher.sysinfo import process_tree_memory
from sunset_launcher.ui import INSTALL_PAGE, LIBRARY_PAGE, WARMUP_PAGE, MainWindow
from sunset_launcher.warmup import WarmupTracker, format_bytes, format_duration


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class TrackerTests(unittest.TestCase):
    def test_speed_and_time_left_follow_recent_progress(self):
        clock = Clock()
        tracker = WarmupTracker(clock)
        tracker.update(WarmupProgress(0, 1000, "shaders"))
        self.assertIsNone(tracker.speed)
        self.assertIsNone(tracker.seconds_left)
        clock.now += 10
        tracker.update(WarmupProgress(200, 1000, "shaders"))
        self.assertEqual(tracker.fraction, 0.2)
        self.assertEqual(tracker.speed, 20)
        self.assertEqual(tracker.seconds_left, 40)

    def test_a_new_file_starts_a_new_estimate(self):
        clock = Clock()
        tracker = WarmupTracker(clock)
        tracker.update(WarmupProgress(10, 100, "containers", "first.bin"))
        clock.now += 10
        tracker.update(WarmupProgress(60, 100, "containers", "first.bin"))
        clock.now += 10
        tracker.update(WarmupProgress(5, 400, "containers", "second.bin"))
        self.assertIsNone(tracker.speed)
        self.assertEqual(tracker.fraction, 5 / 400)

    def test_a_running_count_has_a_speed_but_no_time_left(self):
        clock = Clock()
        tracker = WarmupTracker(clock)
        tracker.update(WarmupProgress(1000, None, "shaders"))
        clock.now += 4
        tracker.update(WarmupProgress(2000, None, "shaders"))
        self.assertEqual(tracker.speed, 250)
        self.assertIsNone(tracker.fraction)
        self.assertIsNone(tracker.seconds_left)

    def test_a_pause_is_left_out_of_the_elapsed_time_and_speed(self):
        clock = Clock()
        tracker = WarmupTracker(clock)
        tracker.update(WarmupProgress(0, 1000, "pipelines"))
        clock.now += 10
        tracker.update(WarmupProgress(100, 1000, "pipelines"))
        tracker.pause()
        clock.now += 300
        self.assertTrue(tracker.paused)
        self.assertEqual(tracker.elapsed, 10)
        tracker.resume()
        self.assertFalse(tracker.paused)
        self.assertIsNone(tracker.speed)  # Measured again after a pause
        clock.now += 5
        tracker.update(WarmupProgress(150, 1000, "pipelines"))
        clock.now += 5
        tracker.update(WarmupProgress(200, 1000, "pipelines"))
        self.assertEqual((tracker.elapsed, tracker.speed), (20, 10))

    def test_formatting(self):
        self.assertEqual([format_duration(value) for value in (None, 9, 69, 3725)], ["—", "9s", "1m 09s", "1h 02m"])
        self.assertEqual(format_bytes(None), "—")
        self.assertEqual(format_bytes(int(9.7 * 2**30)), "9.7 GB")
        self.assertEqual(format_bytes(700 * 2**20), "700 MB")


class MemoryTests(unittest.TestCase):
    def test_process_tree_memory_follows_every_descendant(self):
        page = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096
        with TemporaryDirectory() as directory, patch.object(sys, "platform", "linux"), \
                patch("sunset_launcher.sysinfo.os.sysconf", return_value=page, create=True):
            proc = Path(directory)
            # umu-run (41) starts the runtime container (42) in its own session, which starts Wine and the
            # layer (43). Process 77 is unrelated. Command names can contain spaces and parentheses.
            for pid, name, parent, session, pages in ((41, "umu-run", 1, 41, 100), (42, "pv-adverb (x)", 41, 42, 20),
                                                      (43, "victory.exe", 42, 42, 900), (77, "other", 1, 77, 999)):
                (proc / str(pid)).mkdir()
                (proc / str(pid) / "stat").write_text(f"{pid} ({name}) S {parent} {session} {session} 0 -1 4194560\n")
                (proc / str(pid) / "statm").write_text(f"5000 {pages} 30 1 0 400 0\n")
            (proc / "self").mkdir()
            self.assertEqual(process_tree_memory(41, proc), 1020 * page)
            self.assertEqual(process_tree_memory(43, proc), 900 * page)
            self.assertIsNone(process_tree_memory(12, proc))


class PauseTests(unittest.TestCase):
    """A real process and the child it starts are frozen, continued and stopped together."""

    def test_pause_resume_and_cancel_reach_the_whole_tree(self):
        import subprocess
        import threading
        import time

        from sunset_launcher.backend import Cancelled, Runner
        from sunset_launcher.sysinfo import process_tree

        with TemporaryDirectory() as directory:
            ticks = Path(directory) / "ticks"
            child = (
                "import sys, time\n"
                "with open(sys.argv[1], 'a') as f:\n"
                "    while True:\n"
                "        f.write('x'); f.flush(); time.sleep(0.02)\n"
            )
            parent = (
                "import subprocess, sys\n"
                f"subprocess.Popen([sys.executable, '-c', {child!r}, sys.argv[1]]).wait()\n"
            )
            runner = Runner(lambda text: None)
            ended: list[BaseException] = []

            def work():
                try:
                    runner.run((sys.executable, "-c", parent, str(ticks)), Path(directory))
                except BaseException as error:
                    ended.append(error)

            thread = threading.Thread(target=work)
            thread.start()

            def size():
                return ticks.stat().st_size if ticks.exists() else 0

            deadline = time.monotonic() + 15
            while size() < 5 and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertGreaterEqual(size(), 5, "the child never started")
            self.assertGreaterEqual(len(process_tree(runner.pid)), 2)

            self.assertTrue(runner.pause())
            time.sleep(0.3)
            frozen = size()
            time.sleep(0.5)
            self.assertEqual(size(), frozen, "the child kept running while paused")
            self.assertFalse(runner.pause())  # Already paused

            self.assertTrue(runner.resume())
            time.sleep(0.5)
            self.assertGreater(size(), frozen)

            # A stop while paused wakes the tree first, so it ends.
            self.assertTrue(runner.pause())
            runner.cancel()
            thread.join(timeout=15)
            self.assertFalse(thread.is_alive())
            self.assertIsInstance(ended[0], Cancelled)
            time.sleep(0.5)
            stopped = size()
            time.sleep(0.5)
            self.assertEqual(size(), stopped, "the child outlived the stop")


class PageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(["sunset-launcher-tests", "-platform", "offscreen"])
        apply_theme(cls.app)

    def wait_for(self, condition):
        loop = QEventLoop()
        poll = QTimer()
        poll.setInterval(10)
        poll.timeout.connect(lambda: loop.quit() if condition() else None)
        watchdog = QTimer()
        watchdog.setSingleShot(True)
        watchdog.timeout.connect(loop.quit)
        poll.start()
        watchdog.start(4000)
        try:
            if not condition():
                loop.exec()
            self.assertTrue(condition(), "The launcher job did not reach the expected state.")
        finally:
            poll.stop()
            watchdog.stop()

    def window(self, directory):
        window = MainWindow(Path(directory), scan_runtime=False, check_updates_on_startup=False)
        self.addCleanup(window.close)
        window.show()
        self.app.processEvents()
        return window

    @patch.object(sys, "platform", "linux")  # The full catalog, including Linux-only TuxBox
    def test_install_moves_a_built_layer_into_the_library(self):
        project = next(item for item in PROJECTS if item.key == "victory")
        installed: dict[str, str] = {}

        def build(library, item, update, runner):
            installed[item.key] = "b" * 40
            return installed[item.key]

        with (
            TemporaryDirectory() as directory,
            patch.object(Library, "revision", lambda library, item: installed.get(item.key)),
            patch.object(Library, "build", build),
            patch.object(Library, "fetch_layer_files") as fetch,
        ):
            window = self.window(directory)
            self.assertFalse(window.empty_library.isHidden())
            self.assertTrue(window.library_grid.isHidden())
            available = [item for item in PROJECTS if recipe_for(item) is not None]
            visible_cards = [key for key, card in window.install_cards.items() if not card.isHidden()]
            self.assertEqual(visible_cards, [item.key for item in available])
            self.assertEqual(window.nav.button(INSTALL_PAGE).badge.text(), str(len(available)))
            self.assertEqual(window.nav.button(LIBRARY_PAGE).badge.text(), "")

            window._show_page(INSTALL_PAGE)
            window.install_cards[project.key].install.click()
            self.wait_for(lambda: window.thread is None and project.key in installed)
            self.app.processEvents()
            self.assertTrue(window.install_cards[project.key].isHidden())
            self.assertFalse(window.rows[project.key].isHidden())
            self.assertTrue(all(row.isHidden() for key, row in window.rows.items() if key != project.key))
            self.assertTrue(window.empty_library.isHidden())
            self.assertEqual(window.selected, project)
            # Victory needs no DLSS files, so nothing was asked and only its own files are fetched.
            self.assertEqual(fetch.call_args.kwargs, {"dlss": False})
            self.assertIn("installed", window.summary.text())
            self.assertEqual(window.nav.button(LIBRARY_PAGE).badge.text(), "1")
            self.assertEqual(window.nav.button(INSTALL_PAGE).badge.text(), str(len(available) - 1))

    def test_warm_up_shows_progress_and_remembers_the_warmed_build(self):
        project = next(item for item in PROJECTS if item.key == "xcl")
        if recipe_for(project) is None:
            self.skipTest("The fixture requires a supported build platform")
        calls = []

        def warm_up(library, item, raw_game, proton, runner, **settings):
            calls.append((item.key, raw_game, settings["jobs"]))
            for line in ("precompiling 1000 shaders into cache", "  250/1000 (5 s)", "  1000/1000 (9 s)"):
                runner.output(line + "\n")
            return "c" * 40

        with (
            TemporaryDirectory() as directory,
            patch.object(Library, "revision", lambda library, item: "c" * 40 if item.key == "xcl" else None),
            patch.object(Library, "warm_up", warm_up),
            patch.object(sys, "platform", "win32"),  # A native runtime needs no Proton selection.
        ):
            game = Path(directory) / "Game.exe"
            game.touch()
            window = self.window(directory)
            window.game.setText(str(game))
            window._open_warmup(project)
            self.assertEqual(window.pages.currentIndex(), WARMUP_PAGE)
            self.assertEqual(window.warm_layer.currentData(), project)
            self.assertIn("Not warmed up", window.warm_stage.text())
            self.assertTrue(window._needs_warmup(project, str(game)))
            window.warm_jobs.setCurrentIndex(window.warm_jobs.findData(4))
            self.assertTrue(window.warm_pipelines.isChecked())
            with patch.object(window, "_begin_job") as begin:
                window._start((project,), "warm")
                self.assertTrue(begin.call_args.args[0].warm_pipelines)
                window._start((project,), "launch")
                self.assertFalse(begin.call_args.args[0].warm_pipelines)  # Never before a launch
            window.warm_button.click()
            self.wait_for(lambda: window.thread is None and calls)
            self.app.processEvents()
            self.assertEqual(calls, [("xcl", str(game), 4)])
            self.assertEqual(window.warm_title.text(), "Warm-up finished")
            self.assertEqual(window.warm_percent.text(), "100%")
            self.assertEqual(window.warm_stats["done"].text(), "1,000 / 1,000")
            self.assertEqual(window.warm_button.text(), "Warm up")
            self.assertFalse(window._needs_warmup(project, str(game)))
            self.assertIn("Warmed up for this game", window.warm_stage.text())

    def test_the_warm_up_card_pauses_resumes_and_cancels(self):
        from types import SimpleNamespace
        from unittest.mock import Mock

        with TemporaryDirectory() as directory:
            window = self.window(directory)
            runner = Mock(pause=Mock(return_value=True), resume=Mock(return_value=True))
            window.worker = SimpleNamespace(runner=runner)
            window.warm_key = "xcl"
            window.warm_tracker.reset()
            window._refresh_actions()  # As a job's start does
            window._update_warm_card()
            self.assertEqual(window.warm_button.text(), "Pause")
            self.assertFalse(window.warm_cancel.isHidden())
            window.warm_button.click()
            runner.pause.assert_called_once()
            self.assertEqual((window.warm_title.text(), window.warm_button.text()), ("Paused", "Resume"))
            window.warm_button.click()
            runner.resume.assert_called_once()
            self.assertEqual(window.warm_button.text(), "Pause")
            window.warm_button.click()
            window.warm_cancel.click()
            runner.cancel.assert_called_once()
            self.assertFalse(window.warm_tracker.paused)
            window.worker = None
            window.warm_key = None
            window._update_warm_card()
            self.assertTrue(window.warm_cancel.isHidden())
            self.assertEqual(window.warm_button.text(), "Warm up")

    def test_a_failed_warm_up_leaves_the_build_status_alone(self):
        project = next(item for item in PROJECTS if item.key == "xcl")

        def warm_up(library, item, raw_game, proton, runner, **settings):
            raise OperationError("victory.exe exited with code 3.")

        with (
            TemporaryDirectory() as directory,
            patch.object(Library, "revision", lambda library, item: "c" * 40 if item.key == "xcl" else None),
            patch.object(Library, "warm_up", warm_up),
            patch.object(sys, "platform", "win32"),
        ):
            game = Path(directory) / "Game.exe"
            game.touch()
            window = self.window(directory)
            window.game.setText(str(game))
            window.update_checks["xcl"] = ("Up to date", "")
            window._open_warmup(project)
            window.warm_button.click()
            self.wait_for(lambda: window.thread is None)
            self.app.processEvents()
            self.assertEqual(window.warm_title.text(), "Warm-up did not finish")
            self.assertEqual(window.rows["xcl"].status.text(), "Up to date")
            self.assertTrue(window._needs_warmup(project, str(game)))

    def test_launch_warms_up_first_only_after_a_change(self):
        project = next(item for item in PROJECTS if item.key == "xcl")
        events = []
        revision = {"value": "d" * 40}

        def warm_up(library, item, raw_game, proton, runner, **settings):
            events.append("warm")
            return revision["value"]

        def launch(library, item, raw_game, proton, runner, **settings):
            events.append("launch")

        with (
            TemporaryDirectory() as directory,
            patch.object(Library, "revision", lambda library, item: revision["value"] if item.key == "xcl" else None),
            patch.object(Library, "warm_up", warm_up),
            patch.object(Library, "launch", launch),
            patch.object(Library, "shader_usage", return_value=0),
            patch.object(sys, "platform", "win32"),
        ):
            game = Path(directory) / "Game.exe"
            game.touch()
            window = self.window(directory)
            window._select(project)
            window.game.setText(str(game))
            for expected in (["warm", "launch"], ["launch"]):
                events.clear()
                window.launch.click()
                self.wait_for(lambda: window.thread is None and "launch" in events)
                self.assertEqual(events, expected)
            # A rebuilt layer warms up again before its next launch.
            revision["value"] = "e" * 40
            events.clear()
            window.launch.click()
            self.wait_for(lambda: window.thread is None and "launch" in events)
            self.assertEqual(events, ["warm", "launch"])
            # With the setting off, Launch starts the game directly.
            revision["value"] = "f" * 40
            window.warm_before_launch.setChecked(False)
            events.clear()
            window.launch.click()
            self.wait_for(lambda: window.thread is None and "launch" in events)
            self.assertEqual(events, ["launch"])


if __name__ == "__main__":
    unittest.main()
