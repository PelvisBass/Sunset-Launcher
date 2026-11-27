"""Find and install the Windows build tools, with stand-ins for vswhere, winget and the file system."""

import base64
import os
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication

from sunset_launcher import winsetup
from sunset_launcher.__main__ import apply_theme
from sunset_launcher.backend import Cancelled, Library, OperationError, SetupRequired
from sunset_launcher.catalog import PROJECTS
from sunset_launcher.ui import ACTIVITY_PAGE, MainWindow
from sunset_launcher.winsetup import BuildTool


def program(folder: Path, name: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (f"{name}.exe" if os.name == "nt" else name)
    path.write_text("")
    path.chmod(0o755)
    return path


class FakeSystem:
    """A Windows layout under a temporary folder. Environment names are upper-cased, as os.environ has them."""

    def __init__(self, root: Path):
        self.root = root
        self.x86 = root / "Program Files (x86)"
        self.installer = self.x86 / "Microsoft Visual Studio" / "Installer"
        self.profile = root / "Users" / "player"
        self.bin = root / "bin"
        self.bin.mkdir(parents=True)
        self.env = {
            "PATH": str(self.bin), "PROGRAMFILES(X86)": str(self.x86), "PROGRAMFILES": str(root / "Program Files"),
            "USERPROFILE": str(self.profile), "LOCALAPPDATA": str(self.profile / "AppData" / "Local"),
        }
        self.with_tools: list[str] = []
        self.installs: list[str] = []
        self.queries: list[list[str]] = []

    def add_visual_studio(self, path: str, *, tools: bool):
        self.installer.mkdir(parents=True, exist_ok=True)
        (self.installer / "vswhere.exe").write_text("")
        (self.installer / "setup.exe").write_text("")
        self.installs.append(path)
        if tools:
            self.with_tools.append(path)

    def add_sdk(self):
        lib = self.x86 / "Windows Kits" / "10" / "Lib" / "10.0.26100.0" / "um" / "x64"
        lib.mkdir(parents=True)
        (lib / "kernel32.lib").write_text("")

    def run(self, command, **options):
        self.queries.append(command)
        self.assertion_options = options
        paths = self.with_tools if "-requires" in command else self.installs
        return subprocess.CompletedProcess(command, 0, stdout="".join(f"{path}\r\n" for path in paths))


class FakeRunner:
    def __init__(self, on_run=None, fail=()):
        self.lines: list[str] = []
        self.output = self.lines.append
        self.commands: list[tuple[str, ...]] = []
        self.on_run = on_run
        self.fail = fail

    def check_cancelled(self):
        pass

    def run(self, command, cwd, *, env=None, capture=False, timeout=None):
        self.commands.append(tuple(command))
        if self.on_run is not None:
            self.on_run(tuple(command), self.output)
        if any(word in command for word in self.fail):
            raise OperationError(f"{Path(command[0]).name} exited with code 1.")
        return ""


def decoded_script(command) -> str:
    return base64.b64decode(command[command.index("-EncodedCommand") + 1]).decode("utf-16-le")


class DetectionTests(unittest.TestCase):
    def test_everything_installed(self):
        with TemporaryDirectory() as directory:
            system = FakeSystem(Path(directory))
            program(system.bin, "git")
            program(system.profile / ".cargo" / "bin", "cargo")  # Not on PATH yet: installed after the launcher started
            system.add_visual_studio(r"C:\BuildTools", tools=True)
            system.add_sdk()
            tools = winsetup.detect(system.env, system.run)
            self.assertEqual([tool.key for tool in tools], ["git", "msvc", "rust"])
            self.assertEqual(winsetup.missing(tools), ())
            self.assertIn(winsetup.VC_TOOLS, system.queries[0])
            self.assertIn("-products", system.queries[0])

    def test_nothing_installed_installs_build_tools_with_the_cpp_workload(self):
        with TemporaryDirectory() as directory:
            system = FakeSystem(Path(directory))
            tools = winsetup.detect(system.env, system.run)
            self.assertEqual([tool.key for tool in winsetup.missing(tools)], ["git", "msvc", "rust"])
            msvc = tools[1]
            self.assertEqual(msvc.detail, "Not installed")
            command = winsetup.install_command(msvc, "winget.exe", system.env)
            self.assertEqual(command[:4], ("winget.exe", "install", "--id", "Microsoft.VisualStudio.BuildTools"))
            self.assertIn("--accept-package-agreements", command)
            override = command[command.index("--override") + 1]
            for part in ("--passive", "--wait", "Microsoft.VisualStudio.Workload.VCTools", "--includeRecommended"):
                self.assertIn(part, override)
            git = winsetup.install_command(tools[0], "winget.exe", system.env)
            self.assertEqual(git[3], "Git.Git")
            self.assertIn("--silent", git)

    def test_visual_studio_without_cpp_tools_is_modified_in_place(self):
        with TemporaryDirectory() as directory:
            system = FakeSystem(Path(directory))
            install = r"C:\Program Files\Microsoft Visual Studio\2026\Community"
            system.add_visual_studio(install, tools=False)
            system.add_sdk()
            msvc = winsetup.detect(system.env, system.run)[1]
            self.assertFalse(msvc.found)
            self.assertEqual(msvc.detail, "Visual Studio is installed without the C++ tools")
            command = winsetup.install_command(msvc, "winget.exe", system.env)
            self.assertEqual(command[0], "powershell.exe")
            script = decoded_script(command)
            self.assertIn("-Verb RunAs -Wait", script)
            self.assertIn(str(system.installer / "setup.exe"), script)
            self.assertIn(f'modify --installPath "{install}" --add Microsoft.VisualStudio.Workload.VCTools', script)

    def test_cpp_tools_without_an_sdk_are_still_missing(self):
        with TemporaryDirectory() as directory:
            system = FakeSystem(Path(directory))
            system.add_visual_studio(r"C:\BuildTools", tools=True)
            msvc = winsetup.detect(system.env, system.run)[1]
            self.assertFalse(msvc.found)
            self.assertEqual(msvc.detail, "The Windows SDK is missing")
            self.assertEqual(msvc.install_path, r"C:\BuildTools")

    def test_tool_folders_join_path_once(self):
        with TemporaryDirectory() as directory:
            system = FakeSystem(Path(directory))
            cargo = system.profile / ".cargo" / "bin"
            cargo.mkdir(parents=True)
            env = winsetup.with_tool_folders(system.env)
            self.assertEqual(env["PATH"].split(os.pathsep), [str(system.bin), str(cargo)])
            self.assertEqual(winsetup.with_tool_folders(env)["PATH"], env["PATH"])


class InstallTests(unittest.TestCase):
    def test_missing_tools_install_in_order_and_ignore_winget_exit_codes(self):
        with TemporaryDirectory() as directory:
            system = FakeSystem(Path(directory))
            winget = program(system.profile / "AppData" / "Local" / "Microsoft" / "WindowsApps", "winget")

            def installed(command, output):
                if os.path.normcase(command[0]) == os.path.normcase(str(winget)):
                    output("   -\n  ██████▒▒▒▒  10.0 MB / 60.0 MB\n Successfully installed\n")
                    package = command[command.index("--id") + 1]
                    if package == "Git.Git":
                        program(system.bin, "git")
                    elif package == "Rustlang.Rustup":
                        program(system.profile / ".cargo" / "bin", "cargo")
                        program(system.profile / ".cargo" / "bin", "rustup")
                    else:
                        system.add_visual_studio(r"C:\BuildTools", tools=True)
                        system.add_sdk()

            # winget fails Git because it was "already installed", and rustup has no default toolchain yet.
            runner = FakeRunner(installed, fail=("Git.Git", "active-toolchain"))
            winsetup.install(runner, system.env, system.run)
            packages = [command[command.index("--id") + 1] for command in runner.commands if "--id" in command]
            self.assertEqual(packages, ["Git.Git", "Microsoft.VisualStudio.BuildTools", "Rustlang.Rustup"])
            self.assertEqual(runner.commands[-1][1:], ("default", "stable-x86_64-pc-windows-msvc"))
            log = "".join(runner.lines)
            self.assertIn("Successfully installed", log)
            self.assertNotIn("█", log)
            self.assertIn("Checking whether Git is installed anyway", log)
            self.assertIn("Build tools are ready", log)

    def test_a_tool_still_missing_after_setup_fails(self):
        with TemporaryDirectory() as directory:
            system = FakeSystem(Path(directory))
            program(system.bin, "winget")
            program(system.bin, "git")
            program(system.bin, "cargo")
            with self.assertRaisesRegex(OperationError, "Still missing after setup: Visual Studio C\\+\\+ Build Tools"):
                winsetup.install(FakeRunner(), system.env, system.run)

    def test_without_winget_setup_names_the_tools(self):
        with TemporaryDirectory() as directory:
            system = FakeSystem(Path(directory))
            with self.assertRaisesRegex(SetupRequired, "winget.*Git, Visual Studio C\\+\\+ Build Tools, Rust"):
                winsetup.install(FakeRunner(), system.env, system.run)

    def test_cancel_stops_setup(self):
        with TemporaryDirectory() as directory:
            system = FakeSystem(Path(directory))
            program(system.bin, "winget")

            def cancel(command, output):
                raise Cancelled("Stopped by user.")

            runner = FakeRunner(cancel)
            with self.assertRaises(Cancelled):
                winsetup.install(runner, system.env, system.run)
            self.assertEqual(len(runner.commands), 1)


@unittest.skipUnless(os.name == "nt", "Reads the real Windows installation")
class RealWindowsTests(unittest.TestCase):
    def test_the_ci_runner_has_every_build_tool(self):
        # GitHub's Windows runners ship Git, rustup and Visual Studio with the C++ tools and a Windows SDK.
        tools = winsetup.detect()
        self.assertEqual([(tool.key, tool.detail) for tool in tools if not tool.found], [])
        self.assertTrue(tools[1].install_path)


MISSING = (
    BuildTool("git", "Git", "about 350 MB", True, "Installed"),
    BuildTool("msvc", "Visual Studio C++ Build Tools", "about 7 GB", False, "Not installed"),
    BuildTool("rust", "Rust", "about 1.5 GB", False, "Not installed"),
)
READY = tuple(BuildTool(tool.key, tool.name, tool.size, True, "Installed") for tool in MISSING)


class BuildCheckTests(unittest.TestCase):
    def test_a_windows_build_without_msvc_needs_setup_before_cloning(self):
        project = next(item for item in PROJECTS if item.key == "victory")
        with (
            TemporaryDirectory() as directory,
            patch.object(sys, "platform", "win32"),
            patch("sunset_launcher.backend.detect_build_tools", return_value=MISSING),
            patch("sunset_launcher.backend.shutil.which", lambda name, path=None: None if name == "cargo" else name),
        ):
            library = Library(Path(directory))
            with self.assertRaisesRegex(SetupRequired, "Missing build tools: Rust, Visual Studio C\\+\\+ Build Tools. "
                                                       "Choose Install missing tools under Build tools"):
                library.build(project, False, FakeRunner())
            self.assertFalse(library.source(project).exists())


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

    def test_linux_has_no_build_tools_card(self):
        with TemporaryDirectory() as directory, patch.object(sys, "platform", "linux"):
            window = MainWindow(Path(directory), scan_runtime=False, check_updates_on_startup=False)
            self.addCleanup(window.close)
            self.assertIsNone(window.build_tools_card)

    def test_installing_a_layer_sets_up_missing_tools_first(self):
        project = next(item for item in PROJECTS if item.key == "victory")
        tools = {"value": MISSING}
        events = []

        def install(runner):
            events.append("tools")
            tools["value"] = READY

        def build(library, item, update, runner):
            events.append(("build", item.key))
            return "b" * 40

        with (
            TemporaryDirectory() as directory,
            patch.object(sys, "platform", "win32"),
            patch.object(winsetup, "detect", lambda: tools["value"]),
            patch.object(winsetup, "winget", return_value="winget.exe"),
            patch.object(winsetup, "install", install),
            patch.object(Library, "build", build),
        ):
            window = MainWindow(Path(directory), scan_runtime=False, check_updates_on_startup=False)
            self.addCleanup(window.close)
            window.show()
            card = window.build_tools_card
            self.assertEqual(card.status.text(), "2 missing")
            self.assertEqual(card.marks["git"].text(), "✓ Installed")
            self.assertEqual(card.marks["msvc"].text(), "Not installed")
            self.assertFalse(card.install.isHidden())

            # Declining installs nothing and builds nothing.
            with patch.object(MainWindow, "_confirm_build_tools", return_value=False) as confirm:
                window.install_cards[project.key].install.click()
            self.assertIn("Victory needs build tools", confirm.call_args.args[0])
            self.assertIsNone(window.thread)
            self.assertEqual(events, [])

            with patch.object(MainWindow, "_confirm_build_tools", return_value=True):
                window.install_cards[project.key].install.click()
            self.assertEqual(window.pages.currentIndex(), ACTIVITY_PAGE)
            self.wait_for(lambda: window.thread is None and len(events) == 2)
            self.app.processEvents()
            self.assertEqual(events, ["tools", ("build", "victory")])
            self.assertEqual(card.status.text(), "Ready")
            self.assertTrue(card.install.isHidden())

    def test_failed_tool_setup_skips_the_build(self):
        project = next(item for item in PROJECTS if item.key == "victory")
        events = []

        def install(runner):
            raise OperationError("Still missing after setup: Rust.")

        with (
            TemporaryDirectory() as directory,
            patch.object(sys, "platform", "win32"),
            patch.object(winsetup, "detect", lambda: MISSING),
            patch.object(winsetup, "winget", return_value="winget.exe"),
            patch.object(winsetup, "install", install),
            patch.object(Library, "build", lambda *args: events.append("build")),
            patch.object(MainWindow, "_confirm_build_tools", return_value=True),
        ):
            window = MainWindow(Path(directory), scan_runtime=False, check_updates_on_startup=False)
            self.addCleanup(window.close)
            window.show()
            window.install_cards[project.key].install.click()
            self.wait_for(lambda: window.thread is None)
            self.app.processEvents()
            self.assertEqual(events, [])
            self.assertEqual(window.summary.text(), "Build tools setup did not finish")
            self.assertEqual(window.install_cards[project.key].status.text(), "Needs setup")
            self.assertIn("Still missing after setup: Rust.", window.console.toPlainText())


if __name__ == "__main__":
    unittest.main()
