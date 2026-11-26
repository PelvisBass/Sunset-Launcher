"""Exercise the launcher with local Git repositories and Python build commands."""

import json
import os
import shutil
import subprocess
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from sunset_launcher import backend
from sunset_launcher.backend import (
    Library, OperationError, Runner, SetupRequired, expand_command, parse_launch_options, recipe_for,
    split_windows_options,
)
from sunset_launcher.catalog import PROJECTS, Recipe, Step


class RecipeTests(unittest.TestCase):
    def test_one_executable_path_is_valid_for_every_project(self):
        with TemporaryDirectory() as directory:
            folder = Path(directory).resolve()
            game = folder / "Game with spaces.exe"
            game.touch()
            library = Library(folder)
            for project in PROJECTS:
                with self.subTest(project=project.name):
                    self.assertEqual(library.game_path(project, str(game)), game)
                    if project.game_input == "either":
                        self.assertEqual(library.game_path(project, str(folder)), folder)
                    else:
                        with self.assertRaisesRegex(OperationError, "Choose the game executable"):
                            library.game_path(project, str(folder))

    def test_linux_and_windows_runtime_selection(self):
        with patch.object(sys, "platform", "linux"):
            self.assertEqual(
                [recipe_for(project).runtime for project in PROJECTS],
                ["proton", "proton", "managed", "proton", "proton", "proton", "proton", "proton"],
            )
        with patch.object(sys, "platform", "win32"):
            self.assertEqual(
                {project.key: recipe_for(project) and recipe_for(project).runtime for project in PROJECTS},
                {"xcl": "native", "victory": "native", "tuxbox": None, "hyphennn": "native",
                 "ztrx": "native", "dexnom": "native", "avatarmanz": "native", "cool_fox": "native"},
            )
        with patch.object(sys, "platform", "darwin"):
            self.assertTrue(all(recipe_for(project) is None for project in PROJECTS))


    def test_package_version_matches_the_project_metadata(self):
        import re
        from sunset_launcher import __version__

        metadata = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8")
        self.assertEqual(re.search(r'^version = "([^"]+)"', metadata, re.MULTILINE)[1], __version__)

    def test_launch_options_split_leading_settings_from_arguments(self):
        self.assertEqual(parse_launch_options(""), ({}, []))
        self.assertEqual(
            parse_launch_options('XCL_NETWORK=1 DXVK_HUD="fps,frametimes" --console series-s A=B'),
            ({"XCL_NETWORK": "1", "DXVK_HUD": "fps,frametimes"}, ["--console", "series-s", "A=B"]),
        )
        with self.assertRaisesRegex(OperationError, "launch options"):
            parse_launch_options('--log-file "unclosed')

    def test_windows_options_keep_backslashes_and_remove_quotes(self):
        self.assertEqual(
            split_windows_options(r"""A="x y" B='it is' --log-file C:\Games\log.txt "C:\My Games\a b" '' x"""),
            ["A=x y", "B=it is", "--log-file", r"C:\Games\log.txt", r"C:\My Games\a b", "", "x"],
        )
        with patch.object(os, "name", "nt"):
            self.assertEqual(
                parse_launch_options("SUNSET_OPTION='two words' --fast 'quoted arg'"),
                ({"SUNSET_OPTION": "two words"}, ["--fast", "quoted arg"]),
            )
            with self.assertRaisesRegex(OperationError, "launch options"):
                parse_launch_options('--log-file "unclosed')

    def test_launch_arguments_go_where_each_layer_reads_them(self):
        values = {"artifact": "layer.exe", "game": "Game.exe", "venv_python": "python", "console_mode": "series-s"}
        options = ["--flag", "value"]
        expected = {
            "xcl": ["layer.exe", "run", "Game.exe", "--flag", "value"],
            "victory": ["layer.exe", "run", "Game.exe", "--execute", "--flag", "value"],
            "hyphennn": ["layer.exe", "run", "Game.exe", "--execute", "--flag", "value"],
            "ztrx": ["layer.exe", "run", "Game.exe", "--execute", "--flag", "value"],
            "dexnom": ["layer.exe", "run", "Game.exe", "--execute", "--console", "series-x", "--unimplemented", "trap", "--flag", "value"],
            "avatarmanz": ["layer.exe", "run", "Game.exe", "--execute", "--flag", "value"],
            "cool_fox": ["layer.exe", "run", "Game.exe", "--console", "series-s", "--execute", "--flag", "value"],
            "tuxbox": ["python", "tuxbox.py", "run", "--flag", "value", "Game.exe"],
        }
        for project in PROJECTS:
            for host, recipe in project.recipes.items():
                with self.subTest(project=project.name, host=host):
                    self.assertTrue(recipe.options_hint)
                    self.assertEqual(expand_command(recipe.command, values, options), expected[project.key])
                    self.assertNotIn("{options}", expand_command(recipe.command, values))

    def test_cool_fox_uses_upstream_release_profile_and_isolated_cache(self):
        project = next(project for project in PROJECTS if project.key == "cool_fox")
        for host, target in (("linux", "x86_64-pc-windows-msvc/"), ("windows", "")):
            with self.subTest(host=host):
                recipe = project.recipes[host]
                self.assertEqual(recipe.artifact, f"target/{target}release/victory.exe")
                self.assertIn("--release", recipe.steps[0].command)
                self.assertIn("--locked", recipe.steps[0].command)
                self.assertEqual(recipe.console_modes, ("series-s", "series-x"))
                self.assertEqual(dict(recipe.launch_env)["VICTORY_CARD_BUFFERS_MIN_KB"], "64")
                self.assertEqual(dict(recipe.launch_paths), {"VICTORY_CACHE_DIR": "cache", "VICTORY_DATA_DIR": "data"})


class BundleHostTests(unittest.TestCase):
    def test_frozen_launcher_uses_system_python(self):
        available = {"python3": "/example/python3", "python": "/example/python"}

        def locate(command, *, path):
            self.assertEqual(path, "/example/host-bin")
            return available.get(command)

        with (
            patch.object(sys, "frozen", True, create=True),
            patch.object(backend, "host_environment", return_value={"PATH": "/example/host-bin"}),
            patch.object(shutil, "which", side_effect=locate),
        ):
            self.assertEqual(backend.host_python(), available["python3"])
            del available["python3"]
            self.assertEqual(backend.host_python(), available["python"])
            available.clear()
            self.assertIsNone(backend.host_python())

    def test_source_uses_its_current_python_and_environment(self):
        environment = {"LD_LIBRARY_PATH": "/example/libraries", "QT_PLUGIN_PATH": "/example/qt"}
        with patch.object(sys, "frozen", False, create=True):
            self.assertEqual(backend.host_python(), sys.executable)
            self.assertEqual(backend.host_environment(environment), environment)

    def test_frozen_linux_restores_the_host_loader_and_filters_bundled_qt_paths(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            bundle = root / "bundle"
            host_qt = root / "bundle-external" / "qt"
            host_bin = root / "system-bin"
            cargo_home = root / "cargo"
            local_bin = root / ".local/bin"
            for path in (cargo_home / "bin", local_bin):
                path.mkdir(parents=True)
            environment = {
                "LD_LIBRARY_PATH": str(bundle),
                "LD_LIBRARY_PATH_ORIG": str(root / "system-libraries"),
                "QT_PLUGIN_PATH": os.pathsep.join((str(bundle / "plugins"), str(host_qt))),
                "QML_IMPORT_PATH": str(bundle / "qml"),
                "QML2_IMPORT_PATH": str(host_qt),
                "OTHER_TOOL_SETTING": "keep",
                "PATH": str(host_bin),
                "CARGO_HOME": str(cargo_home),
            }
            with (
                patch.object(Path, "home", return_value=root),
                patch.object(sys, "platform", "linux"),
                patch.object(sys, "frozen", True, create=True),
                patch.object(sys, "_MEIPASS", str(bundle), create=True),
            ):
                restored = backend.host_environment(environment)
                self.assertEqual(restored["LD_LIBRARY_PATH"], environment["LD_LIBRARY_PATH_ORIG"])
                self.assertEqual(restored["QT_PLUGIN_PATH"], str(host_qt))
                self.assertFalse(restored.get("QML_IMPORT_PATH"))
                self.assertEqual(restored["QML2_IMPORT_PATH"], str(host_qt))
                self.assertEqual(restored["OTHER_TOOL_SETTING"], "keep")
                self.assertEqual(restored["PATH"].split(os.pathsep), list(map(str, (host_bin, cargo_home / "bin", local_bin))))
                self.assertEqual(environment["LD_LIBRARY_PATH"], str(bundle))
                self.assertNotIn("LD_LIBRARY_PATH", backend.host_environment({"LD_LIBRARY_PATH": str(bundle)}))


@unittest.skipUnless(shutil.which("git"), "Git is required for local Git integration tests")
class LibraryTests(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        config = self.root / "git-test.config"
        config.touch()
        git_environment = patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(config), "GIT_CONFIG_NOSYSTEM": "1"})
        git_environment.start()
        self.addCleanup(git_environment.stop)

        self.origin = self.root / "upstream fixture"
        self.origin.mkdir()
        self.git(self.origin, "init", "-q")
        self.git(self.origin, "config", "user.name", "Sunset Launcher Tests")
        self.git(self.origin, "config", "user.email", "sunset-tests@example.invalid")
        (self.origin / "build.py").write_text(
            "from shutil import copyfile\ncopyfile('game_runner.py', 'artifact.py')\n",
            encoding="utf-8",
        )
        (self.origin / ".gitignore").write_text("artifact.py\n", encoding="utf-8")
        self.commit_runner("initial")
        self.game = self.root / "Game {with spaces}.exe"
        self.game.touch()

        recipe = Recipe(
            dependencies=(),
            steps=(Step(("{python}", "build.py"), "Build the fixture"),),
            artifact="artifact.py",
            command=("{python}", "{artifact}", "{game}"),
            runtime="native",
        )
        host = "windows" if sys.platform == "win32" else sys.platform
        self.project = replace(PROJECTS[0], repository=self.origin.as_uri(), recipes={host: recipe})
        self.library = Library(self.root / "Launcher data")
        self.output = []

    def git(self, directory, *arguments):
        result = subprocess.run(
            ["git", "-C", str(directory), *arguments], capture_output=True, text=True, check=True,
        )
        return result.stdout.strip()

    def commit_runner(self, message):
        (self.origin / "game_runner.py").write_text(
            f"import sys\nprint({message!r}, sys.argv[1])\n", encoding="utf-8",
        )
        self.git(self.origin, "add", ".")
        self.git(self.origin, "commit", "-qm", message)
        return self.git(self.origin, "rev-parse", "HEAD")

    def build(self, *, update=False):
        return self.library.build(self.project, update, Runner(self.output.append))

    def test_clone_build_update_and_launch(self):
        first = self.git(self.origin, "rev-parse", "HEAD")
        self.assertEqual(self.build(), first)
        self.assertEqual(self.library.revision(self.project), first)
        self.library.launch(self.project, str(self.game), None, Runner(self.output.append))
        self.assertIn(f"initial {self.game}\n", self.output)

        second = self.commit_runner("updated")
        self.assertNotEqual(first, second)
        self.assertEqual(self.build(update=True), second)
        self.assertEqual(self.library.revision(self.project), second)
        self.library.launch(self.project, str(self.game), None, Runner(self.output.append))
        self.assertIn(f"updated {self.game}\n", self.output)

    def test_launch_options_reach_the_command_and_environment(self):
        self.build()
        recipe = next(iter(self.project.recipes.values()))
        probe = "import os, sys; print('probe', sys.argv[1:], os.environ.get('SUNSET_OPTION'))"
        self.project = replace(
            self.project,
            recipes={host: replace(recipe, command=("{python}", "-c", probe, "{options}", "{game}"),
                                   launch_env=(("SUNSET_OPTION", "recipe default"),))
                     for host in self.project.recipes},
        )
        with patch.dict(os.environ):
            os.environ.pop("SUNSET_OPTION", None)
            self.library.launch(self.project, str(self.game), None, Runner(self.output.append))
        self.assertIn(f"probe {[str(self.game)]!r} recipe default\n", self.output)
        self.library.launch(
            self.project, str(self.game), None, Runner(self.output.append),
            options="SUNSET_OPTION='two words' --fast 'quoted arg'",
        )
        self.assertIn(f"probe {['--fast', 'quoted arg', str(self.game)]!r} two words\n", self.output)
        self.assertIn("Launch settings: SUNSET_OPTION=two words\n", self.output)

    def test_cool_fox_launch_passes_the_selected_console_mode_once(self):
        project = next(project for project in PROJECTS if project.key == "cool_fox")
        runner = Runner(self.output.append)
        with (
            patch.object(sys, "platform", "win32"),
            patch.object(self.library, "revision", return_value="fixture"),
            patch.object(runner, "run") as run,
        ):
            self.library.launch(project, str(self.game), None, runner)
            self.assertEqual(run.call_args.args[0][3:], ["--console", "series-s", "--execute"])
            self.library.launch(project, str(self.game), None, runner, console_mode="series-x", options="-- --console=game")
            self.assertEqual(run.call_args.args[0][3:], ["--console", "series-x", "--execute", "--", "--console=game"])
            with self.assertRaisesRegex(OperationError, "Choose the console mode in the selector"):
                self.library.launch(project, str(self.game), None, runner, options="--console=series-x")
            self.assertEqual(run.call_count, 2)

    def test_check_reports_new_upstream_commit_without_fetching_or_rebuilding(self):
        first = self.build()
        source = self.library.source(self.project)
        fetch_head = source / ".git/FETCH_HEAD"
        before_fetch_head = fetch_head.read_bytes() if fetch_head.is_file() else None
        runner = Runner(self.output.append)
        self.assertEqual(self.library.check_update(self.project, runner).status, "Up to date")

        second = self.commit_runner("upstream update")
        result = self.library.check_update(self.project, runner)
        self.assertEqual(result.status, "Update available")
        self.assertEqual(result.revision, second)
        self.assertEqual(self.git(source, "rev-parse", "HEAD"), first)
        self.assertEqual(self.git(source, "rev-parse", "@{upstream}"), first)
        self.assertEqual(self.library.revision(self.project), first)
        self.assertEqual(fetch_head.read_bytes() if fetch_head.is_file() else None, before_fetch_head)

        self.build(update=True)
        self.assertEqual(self.library.check_update(self.project, runner).status, "Up to date")

    def test_check_uses_checkout_tracking_branch_and_reports_unavailable_remote(self):
        self.build()
        source = self.library.source(self.project)
        self.git(self.origin, "checkout", "-qb", "next")
        next_revision = self.commit_runner("next branch")
        self.git(source, "fetch", "-q", "origin")
        self.git(source, "checkout", "-qb", "local-next", "--track", "origin/next")
        self.build()
        runner = Runner(self.output.append)
        self.assertEqual(self.library.check_update(self.project, runner).revision, next_revision)

        self.origin.rename(self.origin.with_name("unavailable"))
        with self.assertRaisesRegex(OperationError, "git exited"):
            self.library.check_update(self.project, runner)
        self.assertEqual(self.library.revision(self.project), next_revision)

    def test_catalogued_unavailable_upstream_preserves_local_building_and_launch(self):
        original = self.build()
        self.origin.rename(self.origin.with_name("unavailable"))
        self.project = replace(self.project, upstream_unavailable="The original repository is unavailable.")
        runner = Runner(self.output.append)
        with self.assertRaisesRegex(OperationError, "original repository is unavailable"):
            self.library.check_update(self.project, runner)
        with self.assertRaisesRegex(OperationError, "original repository is unavailable"):
            self.library.build(self.project, True, runner)
        self.assertEqual(self.build(), original)
        self.library.launch(self.project, str(self.game), None, runner)
        self.assertIn(f"initial {self.game}\n", self.output)

    def test_unsuccessful_offline_rebuild_restores_the_previous_executable_and_record(self):
        original = self.build()
        self.origin.rename(self.origin.with_name("unavailable"))
        self.project = replace(self.project, upstream_unavailable="The original repository is unavailable.")
        source = self.library.source(self.project)
        artifact = source / "artifact.py"
        record = source.parent / "build.json"
        original_artifact = artifact.read_bytes()
        original_record = record.read_bytes()
        host, recipe = next(iter(self.project.recipes.items()))

        for ending, expected in (("raise SystemExit(7)", OperationError), ("print('cancel')", backend.Cancelled)):
            with self.subTest(ending=ending):
                script = f"from pathlib import Path; Path('artifact.py').write_text('incomplete'); {ending}"
                steps = (Step(("{python}", "-c", script), "Replace the fixture executable"),)
                project = replace(self.project, recipes={host: replace(recipe, steps=steps)})

                def output(line):
                    self.output.append(line)
                    if line == "cancel\n":
                        runner.cancel()

                runner = Runner(output)
                with self.assertRaises(expected):
                    self.library.build(project, False, runner)
                self.assertEqual(artifact.read_bytes(), original_artifact)
                self.assertEqual(record.read_bytes(), original_record)
                self.assertEqual(self.library.revision(project), original)
                self.assertFalse(list(source.parent.glob(".build-backup-*")))

        self.library.launch(self.project, str(self.game), None, Runner(self.output.append))
        self.assertIn(f"initial {self.game}\n", self.output)

    def test_delete_generated_files_keeps_checkout_game_and_modified_outputs(self):
        with (self.origin / "build.py").open("a", encoding="utf-8") as build:
            build.write("from pathlib import Path\n")
            for name in ("provider.dll", "changed.dll"):
                build.write(f"Path({name!r}).write_bytes(b'compiled')\n")
        (self.origin / ".gitignore").write_text("artifact.py\n*.dll\n", encoding="utf-8")
        self.commit_runner("companion binaries")
        host, recipe = next(iter(self.project.recipes.items()))
        recipe = replace(recipe, companion_outputs=("provider.dll", "changed.dll"))
        self.project = replace(self.project, recipes={host: recipe})
        built = self.build()
        source = self.library.source(self.project)
        (source / "changed.dll").write_text("manual replacement", encoding="utf-8")
        for path in (source / "extra.dll", self.library.prefix(self.project) / "saves", self.game):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"user data")

        self.library.delete_build(self.project, Runner(self.output.append))

        self.assertFalse((source / "artifact.py").exists())
        self.assertFalse((source / "provider.dll").exists())
        self.assertEqual((source / "changed.dll").read_text(encoding="utf-8"), "manual replacement")
        self.assertIn("Kept changed output changed.dll.\n", self.output)
        self.assertTrue((source / "game_runner.py").is_file())
        for path in (source / "extra.dll", self.library.prefix(self.project) / "saves", self.game):
            self.assertEqual(path.read_bytes(), b"user data")
        self.assertFalse(self.library.has_build_record(self.project))
        self.assertIsNone(self.library.revision(self.project))
        self.assertEqual(self.build(update=True), built)

    def test_delete_legacy_build_only_removes_its_executable(self):
        built = self.build()
        source = self.library.source(self.project)
        record = source.parent / "build.json"
        record.write_text(json.dumps({"artifact": "artifact.py", "revision": built}), encoding="utf-8")
        os.utime(record, ns=(record.stat().st_atime_ns, max(record.stat().st_mtime_ns, (source / "artifact.py").stat().st_ctime_ns)))
        (source / "other.dll").touch()

        self.library.delete_build(self.project, Runner(self.output.append))

        self.assertFalse((source / "artifact.py").exists())
        self.assertTrue((source / "other.dll").is_file())

    def test_delete_does_not_follow_a_redirected_checkout(self):
        self.build()
        source = self.library.source(self.project)
        elsewhere = self.root / "other checkout"
        source.rename(elsewhere)
        try:
            source.symlink_to(elsewhere, target_is_directory=True)
        except OSError as error:
            self.skipTest(f"Directory symlinks are unavailable: {error}")

        self.library.delete_build(self.project, Runner(self.output.append))

        self.assertTrue((elsewhere / "artifact.py").is_file())
        self.assertTrue(source.is_symlink())

    def test_missing_system_python_is_reported_before_cloning(self):
        with patch.object(backend, "host_python", return_value=None):
            with self.assertRaisesRegex(OperationError, "Install system Python"):
                self.build()
        self.assertFalse(self.library.source(self.project).exists())

    def test_missing_rust_target_stops_before_build_and_preserves_existing_artifact(self):
        built_revision = self.build()
        host, recipe = next(iter(self.project.recipes.items()))
        recipe = replace(recipe, rust_target="x86_64-pc-windows-msvc")
        self.project = replace(self.project, recipes={host: recipe})
        runner = Runner(self.output.append)
        with (
            patch.object(backend.shutil, "which", return_value="tool-is-on-path"),
            patch.object(runner, "run", return_value=str(self.root / "missing-target-lib")),
        ):
            with self.assertRaisesRegex(SetupRequired, "rustup target add x86_64-pc-windows-msvc"):
                self.library.build(self.project, False, runner)
        self.assertEqual(self.library.revision(self.project), built_revision)

    def test_tuxbox_reports_dxc_and_sdk_together_before_cloning(self):
        host, recipe = next(iter(self.project.recipes.items()))
        recipe = replace(recipe, needs_dxc=True, needs_xwin_sdk=True)
        self.project = replace(self.project, recipes={host: recipe})
        with (
            patch.dict(os.environ, {"DXC": str(self.root / "missing-dxc"), "XWIN_SDK_ROOT": str(self.root / "missing-sdk")}),
            self.assertRaisesRegex(SetupRequired, "DirectX Shader Compiler.*Windows SDK/CRT cache"),
        ):
            self.build(update=True)
        self.assertFalse(self.library.source(self.project).exists())

    def test_failed_fetch_preserves_the_previous_build(self):
        first = self.build()
        self.origin.rename(self.origin.with_name("unavailable"))
        with self.assertRaisesRegex(OperationError, "git.*exited with code"):
            self.build(update=True)
        self.assertEqual(self.library.revision(self.project), first)

    def test_update_preserves_local_tracked_edits(self):
        first = self.build(update=True)
        tracked = self.library.source(self.project) / "game_runner.py"
        tracked.write_text("# A local edit\n", encoding="utf-8")
        self.commit_runner("upstream update")

        with self.assertRaisesRegex(OperationError, "local tracked changes"):
            self.build(update=True)
        self.assertEqual(tracked.read_text(encoding="utf-8"), "# A local edit\n")
        self.assertEqual(self.library.revision(self.project), first)

    def test_successful_command_without_artifact_invalidates_previous_build(self):
        self.build()
        host, recipe = next(iter(self.project.recipes.items()))
        broken_recipe = replace(
            recipe, steps=(Step(("{python}", "-c", "print('no application output')"), "Empty build"),),
        )
        self.project = replace(self.project, recipes={host: broken_recipe})
        with self.assertRaisesRegex(OperationError, "did not produce artifact.py"):
            self.build()
        self.assertIsNone(self.library.revision(self.project))
