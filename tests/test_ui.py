"""Start Qt offscreen and exercise project settings without touching user data."""

import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from PySide6.QtCore import QEventLoop, QSettings, QTimer
from PySide6.QtWidgets import QApplication, QMessageBox

from sunset_launcher.__main__ import apply_theme
from sunset_launcher.backend import UpdateCheck, recipe_for
from sunset_launcher.proton import ProtonInstall
from sunset_launcher.ui import Hero, MainWindow


class DesktopTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(["sunset-launcher-tests", "-platform", "offscreen"])
        apply_theme(cls.app)

    @patch.object(sys, "platform", "linux")  # The full catalog, including Linux-only TuxBox
    def test_project_selection_and_installed_artwork(self):
        with TemporaryDirectory() as directory:
            data = Path(directory)
            test_proton = ProtonInstall("Test Proton", data / "proton")
            with patch("sunset_launcher.ui.discover_proton", return_value=[test_proton]):
                window = MainWindow(data, check_updates_on_startup=False)
                try:
                    window.show()
                    self.app.processEvents()
                    self.assertFalse(window.findChild(Hero).art.isNull())
                    self.assertEqual(window.selected.key, "xcl")
                    self.assertEqual(window.runtime_box.isVisible(), sys.platform.startswith("linux"))
                    if sys.platform.startswith("linux"):
                        window.proton.setCurrentIndex(1)
                    game_path = str(data / "My Game.exe")
                    window.game.setText(game_path)

                    window.rows["tuxbox"].click()
                    self.assertEqual(window.selected.key, "tuxbox")
                    self.assertTrue(window.runtime_box.isHidden())
                    self.assertEqual(window.game.text(), game_path)
                    game_path = str(data / "Other Game.exe")
                    window.game.setText(game_path)
                    window.rows["xcl"].click()
                    self.assertEqual(window.game.text(), game_path)
                    if sys.platform.startswith("linux"):
                        self.assertEqual(window.proton.currentData(), test_proton)
                finally:
                    window.close()

    def test_windows_hides_linux_only_projects_and_proton_controls(self):
        with TemporaryDirectory() as directory, patch.object(sys, "platform", "win32"):
            window = MainWindow(Path(directory), scan_runtime=False, check_updates_on_startup=False)
            try:
                window.show()
                self.app.processEvents()
                self.assertNotIn("tuxbox", window.rows)
                self.assertEqual([project.key for project in window.project_list.projects()],
                                 ["xcl", "victory", "hyphennn", "ztrx"])
                for key, row in window.rows.items():
                    row.click()
                    self.assertTrue(window.runtime_box.isHidden(), key)
                    self.assertEqual(row.runtime.text(), "Native")
                    self.assertNotIn("DXVK", window.launch_options.placeholderText())
            finally:
                window.close()

    def test_newer_launcher_release_is_offered(self):
        from sunset_launcher import __version__
        from sunset_launcher.ui import latest_release

        major, minor, patch_level = map(int, __version__.split("."))
        newer = f"v{major}.{minor + 1}.0"
        listing = "\n".join(f"{'a' * 40}\trefs/tags/{tag}" for tag in (f"v{__version__}", newer, "v0.0.9", "nightly"))
        self.assertEqual(latest_release(listing), newer)
        self.assertIsNone(latest_release(f"{'a' * 40}\trefs/tags/v{__version__}"))
        self.assertIsNone(latest_release(""))
        with TemporaryDirectory() as directory:
            window = MainWindow(Path(directory), scan_runtime=False, check_updates_on_startup=False)
            try:
                window.show()
                self.assertTrue(window.launcher_update.isHidden())
                window._show_launcher_release(newer)
                self.assertFalse(window.launcher_update.isHidden())
                self.assertIn(newer, window.launcher_update.text())
            finally:
                window.close()

    @patch.object(sys, "platform", "linux")  # The full catalog, including Linux-only TuxBox
    def test_launch_options_are_saved_per_project(self):
        with TemporaryDirectory() as directory:
            data = Path(directory)
            window = MainWindow(data, scan_runtime=False, check_updates_on_startup=False)
            try:
                window.rows["xcl"].click()
                self.assertIn("XCL_NETWORK", window.launch_options.placeholderText())
                window.launch_options.setText("XCL_NETWORK=1")
                window.rows["tuxbox"].click()
                self.assertEqual(window.launch_options.text(), "")
                self.assertIn("--mangohud", window.launch_options.placeholderText())
                window.launch_options.setText("--offline")
                window.rows["xcl"].click()
                self.assertEqual(window.launch_options.text(), "XCL_NETWORK=1")
            finally:
                window.close()
            window = MainWindow(data, scan_runtime=False, check_updates_on_startup=False)
            try:
                window.rows["tuxbox"].click()
                self.assertEqual(window.launch_options.text(), "--offline")
            finally:
                window.close()

    def test_shared_game_path_migrates_an_executable_and_remembers_clear(self):
        with TemporaryDirectory() as directory:
            data = Path(directory)
            executable = data / "Game with spaces.exe"
            executable.touch()
            settings = QSettings(str(data / "preferences.ini"), QSettings.Format.IniFormat)
            settings.setValue("project", "xcl")
            settings.setValue("game/xcl", str(data))
            settings.setValue("game/victory", str(executable))
            settings.sync()

            window = MainWindow(data, scan_runtime=False, check_updates_on_startup=False)
            try:
                self.assertEqual(window.game.text(), str(executable))
                window.rows["victory"].click()
                self.assertEqual(window.game.text(), str(executable))
            finally:
                window.close()

            window = MainWindow(data, scan_runtime=False, check_updates_on_startup=False)
            try:
                self.assertEqual(window.game.text(), str(executable))
                window.game.clear()
            finally:
                window.close()

            window = MainWindow(data, scan_runtime=False, check_updates_on_startup=False)
            try:
                self.assertEqual(window.game.text(), "")
                self.assertEqual(window.settings.value("game/xcl"), str(data))
            finally:
                window.close()

    @patch.object(sys, "platform", "linux")  # The full catalog, including Linux-only TuxBox
    def test_project_order_persists_and_drives_update_all(self):
        with TemporaryDirectory() as directory:
            data = Path(directory)
            window = MainWindow(data, scan_runtime=False, check_updates_on_startup=False)
            window.settings.setValue("projectOrder", ["victory", "retired", "victory", "xcl"])
            window.close()
            window = MainWindow(data, scan_runtime=False, check_updates_on_startup=False)
            try:
                self.assertEqual([project.key for project in window.project_list.projects()],
                                 ["victory", "xcl", "tuxbox", "hyphennn", "ztrx"])
                window.project_list.move_row(window.rows["ztrx"], 0)
                window.project_list.move_row(window.rows["victory"], 5)
                self.assertEqual(window.selected.key, "xcl")
            finally:
                window.close()
            window = MainWindow(data, scan_runtime=False, check_updates_on_startup=False)
            try:
                projects = window.project_list.projects()
                self.assertEqual([project.key for project in projects], ["ztrx", "xcl", "tuxbox", "hyphennn", "victory"])
                with patch.object(window, "_start") as start:
                    window.update_all.click()
                    supported = tuple(project for project in projects if recipe_for(project))
                    if supported:
                        start.assert_called_once_with(supported, "update")
                    else:
                        start.assert_not_called()
            finally:
                window.close()

    def test_check_button_compares_built_projects_and_shows_upstream_commit(self):
        with TemporaryDirectory() as directory:
            window = MainWindow(Path(directory), scan_runtime=False, check_updates_on_startup=False)
            project = window.selected
            before, after = "a" * 40, "b" * 40
            try:
                self.assertFalse(window.check_updates.isEnabled())
                with (
                    patch.object(window.library, "revision", side_effect=lambda item: before if item == project else None),
                    patch.object(window.library, "check_update", return_value=UpdateCheck(after, "Update available")) as check,
                ):
                    window._refresh()
                    self.assertTrue(window.check_updates.isEnabled())
                    window.check_updates.click()
                    self.assertFalse(window.check_updates.isEnabled())
                    finished = QEventLoop()
                    window.thread.finished.connect(finished.quit)
                    QTimer.singleShot(3000, finished.quit)
                    finished.exec()
                    self.assertEqual(check.call_count, 1)
                    self.assertEqual(window.rows[project.key].status.text(), "Update available")
                    self.assertIn(after, window.revision.toolTip())
                    self.assertEqual(window.summary.text(), "Update check complete")
            finally:
                window.close()

    def test_delete_requires_confirmation_and_refreshes_build_status(self):
        with TemporaryDirectory() as directory:
            window = MainWindow(Path(directory), scan_runtime=False, check_updates_on_startup=False)
            project = window.selected
            recipe = recipe_for(project)
            if recipe is None:
                window.close()
                self.skipTest("The fixture requires a supported build platform")
            self.assertFalse(window.delete.isEnabled())
            source = window.library.source(project)
            artifact = source / recipe.artifact
            artifact.parent.mkdir(parents=True)
            artifact.write_bytes(b"fixture")
            (source.parent / "build.json").write_text(
                json.dumps({"artifact": recipe.artifact, "revision": "fixture",
                            "outputs": {recipe.artifact: window.library._digest(artifact)}}), encoding="utf-8",
            )
            try:
                window.show()
                window._refresh()
                self.assertTrue(window.delete.isEnabled())
                QTimer.singleShot(0, lambda: self.app.activeModalWidget().button(QMessageBox.StandardButton.Cancel).click())
                window.delete.click()
                self.assertTrue(artifact.is_file())
                self.assertIsNone(window.thread)

                def confirm():
                    dialog = self.app.activeModalWidget()
                    for choice in dialog.buttons():
                        if dialog.buttonRole(choice) == QMessageBox.ButtonRole.DestructiveRole:
                            choice.click()
                            break

                QTimer.singleShot(0, confirm)
                window.delete.click()
                finished = QEventLoop()
                watchdog = QTimer()
                watchdog.setSingleShot(True)
                watchdog.timeout.connect(finished.quit)
                if window.thread is not None:
                    window.thread.finished.connect(finished.quit)
                    watchdog.start(3000)
                    finished.exec()
                self.assertIsNone(window.thread)
                self.assertEqual(window.rows[project.key].status.text(), "Not built")
                self.assertFalse(window.delete.isEnabled())
                self.assertFalse(artifact.exists())
            finally:
                window.close()
