"""Exercise the automatic update check and its interaction with foreground jobs."""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from unittest.mock import patch

from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication, QMessageBox

from sunset_launcher.backend import Library, OperationError, UpdateCheck, recipe_for
from sunset_launcher.catalog import PROJECTS
from sunset_launcher.ui import MainWindow


class StartupUpdateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(["sunset-launcher-tests", "-platform", "offscreen"])

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

    def test_startup_checks_built_projects_once_without_rotating_logs(self):
        first, second, *_ = PROJECTS
        latest = "b" * 40
        with TemporaryDirectory() as directory:
            data = Path(directory)
            logs = data / "logs"
            logs.mkdir()
            filenames = {f"20261124-122000-{number:06d}.log" for number in range(2)}
            for name in filenames:
                (logs / name).write_text("Previous job\n", encoding="utf-8")

            def check_project(project, runner):
                if project == second:
                    raise OperationError("Network unavailable")
                return UpdateCheck(latest, "Update available")

            with (
                patch.object(Library, "revision", side_effect=lambda item: "a" * 40 if item in (first, second) else None),
                patch.object(Library, "check_update", side_effect=check_project) as check,
            ):
                window = MainWindow(data, scan_runtime=False)
                try:
                    check.assert_not_called()
                    window.show()
                    self.wait_for(lambda: check.call_count == 2 and window.thread is None)
                    self.assertEqual([call.args[0] for call in check.call_args_list], [first, second])
                    self.assertEqual(window.rows[first.key].status.text(), "Update available")
                    self.assertIn(latest, window.rows[first.key].toolTip())
                    self.assertEqual(window.rows[second.key].status.text(), "Check unavailable")
                    self.assertTrue(window.check_updates.isEnabled())
                    self.assertEqual({path.name for path in logs.iterdir()}, filenames)
                    window.hide()
                    window.show()
                    self.app.processEvents()
                    self.assertEqual(check.call_count, 2)
                finally:
                    window.close()

    def test_focused_build_can_preempt_the_startup_check(self):
        project = PROJECTS[0]
        if recipe_for(project) is None:
            self.skipTest("The fixture requires a supported build platform")
        started = Event()
        cancelled = Event()

        def wait_for_cancellation(item, runner):
            started.set()
            if runner.stopped.wait(2):
                cancelled.set()
            runner.check_cancelled()
            raise OperationError("The startup check was not cancelled")

        with (
            TemporaryDirectory() as directory,
            patch.object(Library, "revision", side_effect=lambda item: "a" * 40 if item == project else None),
            patch.object(Library, "check_update", side_effect=wait_for_cancellation),
            patch.object(Library, "build", return_value="b" * 40) as build,
        ):
            window = MainWindow(Path(directory), scan_runtime=False)
            try:
                window.show()
                self.wait_for(started.is_set)
                self.assertTrue(window.build.isEnabled())
                self.assertFalse(window.check_updates.isEnabled())
                window.build.click()
                self.wait_for(lambda: build.called and window.thread is None)
                self.assertTrue(cancelled.is_set())
                self.assertEqual(build.call_args.args[:2], (project, False))
                self.assertIn("Build finished.", window.console.toPlainText())
            finally:
                window.close()

    def test_startup_measures_shader_caches_after_the_update_check(self):
        built = PROJECTS[0]
        # Only installed layers are measured: here, the one built project.
        cached = [built] if (recipe := recipe_for(built)) and recipe.shader_cache else []
        if built not in cached:
            self.skipTest("The fixture requires a supported build platform")
        order = []

        def check_project(project, runner):
            order.append(("check", project.key))
            return UpdateCheck("a" * 40, "Up to date")

        def measure(project, runner):
            order.append(("scan", project.key))
            return 3 * 1024 * 1024

        with (
            TemporaryDirectory() as directory,
            patch.object(Library, "revision", side_effect=lambda item: "a" * 40 if item == built else None),
            patch.object(Library, "check_update", side_effect=check_project),
            patch.object(Library, "shader_usage", side_effect=measure) as usage,
        ):
            window = MainWindow(Path(directory), scan_runtime=False)
            try:
                window.show()
                self.wait_for(lambda: usage.call_count == len(cached) and window.thread is None)
                self.assertEqual(order[0], ("check", built.key))
                self.assertEqual({key for action, key in order[1:]}, {project.key for project in cached})
                self.assertIn("3", window.shader_cache_size.text())
                self.assertTrue(window.clear_shaders.isEnabled())
                window.measure_shaders.click()
                self.wait_for(lambda: usage.call_count == len(cached) + 1 and window.thread is None)
                window.hide()
                window.show()
                self.app.processEvents()
                self.assertEqual(usage.call_count, len(cached) + 1)
            finally:
                window.close()

    def test_close_cancels_startup_check_without_a_prompt(self):
        started = Event()

        def wait_for_cancellation(project, runner):
            started.set()
            runner.stopped.wait(2)
            runner.check_cancelled()
            raise OperationError("The startup check was not cancelled")

        with (
            TemporaryDirectory() as directory,
            patch.object(Library, "revision", side_effect=lambda item: "a" * 40 if item == PROJECTS[0] else None),
            patch.object(Library, "check_update", side_effect=wait_for_cancellation),
            patch.object(QMessageBox, "question") as prompt,
        ):
            window = MainWindow(Path(directory), scan_runtime=False)
            try:
                window.show()
                self.wait_for(started.is_set)
                window.close()
                self.wait_for(lambda: window.thread is None and not window.isVisible())
                prompt.assert_not_called()
            finally:
                window.close()
