"""Test Linux Proton discovery and launch arguments without installing Proton."""

import os
import sys
import unittest
from pathlib import Path, PureWindowsPath
from tempfile import TemporaryDirectory
from unittest.mock import patch

from sunset_launcher.proton import (
    ProtonInstall, custom_proton, discover_proton, prepared_wine_launch, proton_game_path, proton_launch,
)


class ProtonPlatformTests(unittest.TestCase):
    def test_windows_does_not_search_for_proton(self):
        with patch.object(sys, "platform", "win32"):
            self.assertEqual(discover_proton(), [])


@unittest.skipUnless(sys.platform.startswith("linux"), "Proton discovery and launch are Linux features")
class ProtonLinuxTests(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        self.steam = self.home / ".local/share/Steam"
        self.extra_tools = self.root / "Custom tools"
        home = patch.object(Path, "home", return_value=self.home)
        environment = patch.dict(os.environ, {"STEAM_EXTRA_COMPAT_TOOLS_PATHS": str(self.extra_tools)}, clear=True)
        for item in (home, environment):
            item.start()
            self.addCleanup(item.stop)

    @staticmethod
    def fake_executable(path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        path.chmod(0o755)
        return path

    def test_discovers_library_and_custom_proton_and_deduplicates_aliases(self):
        secondary = self.root / "Secondary Steam library"
        experimental = self.fake_executable(secondary / "steamapps/common/Proton Experimental/proton")
        external = self.fake_executable(self.extra_tools / "Custom Proton/proton")
        flatpak = self.fake_executable(
            self.home / ".var/app/com.valvesoftware.Steam/data/Steam/compatibilitytools.d/Flatpak Proton/proton",
        )
        (self.steam / "steamapps").mkdir(parents=True)
        (self.steam / "steamapps/libraryfolders.vdf").write_text(
            f'"libraryfolders"\n{{\n"1"\n{{\n"path" "{secondary}"\n}}\n}}\n',
            encoding="utf-8",
        )
        (experimental.parent / "compatibilitytool.vdf").write_text(
            '"display_name" "Fixture Experimental"\n', encoding="utf-8",
        )
        alias = self.steam / "compatibilitytools.d/Experimental alias"
        alias.parent.mkdir(parents=True)
        alias.symlink_to(experimental.parent, target_is_directory=True)

        installations = [item for item in discover_proton() if item.executable.is_relative_to(self.root)]
        self.assertCountEqual([item.executable for item in installations], [experimental, external, flatpak])
        selected = custom_proton(experimental.parent)
        self.assertEqual(selected.name, "Fixture Experimental")
        self.assertEqual(selected.steam_root, self.steam)
        self.assertEqual(next(item.source for item in installations if item.executable == flatpak), "Steam Flatpak")

    def test_umu_launch_uses_selected_version_and_project_prefix(self):
        proton = self.fake_executable(self.steam / "steamapps/common/Proton Fixture/proton")
        (proton.parent / "toolmanifest.vdf").touch()
        install = ProtonInstall("Fixture", proton, self.steam)
        exe = self.root / "Release Builds" / "fixture.exe"
        game = self.root / "Local games" / "Game title.exe"
        compat_data = self.root / "Project data" / "compatdata"
        windows_game = proton_game_path(game)

        with patch("sunset_launcher.proton.shutil.which", return_value="/tools/umu-run"):
            argv, env = proton_launch(install, exe, ["run", windows_game], compat_data, game_path=game)

        self.assertEqual(argv, ["/tools/umu-run", str(exe), "run", windows_game])
        self.assertEqual(PureWindowsPath(windows_game).drive, "Z:")
        self.assertEqual(PureWindowsPath(windows_game).name, game.name)
        self.assertTrue(compat_data.is_dir())
        self.assertEqual(env["STEAM_COMPAT_DATA_PATH"], str(compat_data))
        self.assertEqual(env["WINEPREFIX"], str(compat_data))
        self.assertEqual(env["PROTONPATH"], str(proton.parent))
        self.assertEqual(env["PRESSURE_VESSEL_FILESYSTEMS_RO"], str(game.parent))

    def test_warm_up_verb_reaches_umu_and_direct_proton(self):
        proton = self.fake_executable(self.steam / "steamapps/common/Proton Fixture/proton")
        (proton.parent / "toolmanifest.vdf").touch()
        install = ProtonInstall("Fixture", proton, self.steam)
        exe = self.root / "layer.exe"
        prefix = self.root / "compatdata"
        with patch("sunset_launcher.proton.shutil.which", return_value="/tools/umu-run"):
            _, env = proton_launch(install, exe, [], prefix, verb="runinprefix")
        self.assertEqual(env["PROTON_VERB"], "runinprefix")
        with patch("sunset_launcher.proton.shutil.which", return_value=None):
            argv, _ = proton_launch(install, exe, ["shaders"], prefix, verb="runinprefix")
        self.assertEqual(argv[:3], [str(proton), "runinprefix", str(exe)])
        with patch("sunset_launcher.proton.shutil.which", return_value=None):
            argv, _ = proton_launch(install, exe, [], prefix)
        self.assertEqual(argv[1], "run")

    def test_prepared_wine_preserves_required_dll_overrides(self):
        proton = self.fake_executable(self.root / "Proton Fixture/proton")
        wine = self.fake_executable(proton.parent / "files/bin/wine")
        prefix = self.root / "Prepared Wine prefix"
        (prefix / "drive_c").mkdir(parents=True)
        (prefix / "system.reg").touch()
        exe = self.root / "xcl.exe"
        override = "d3d12,d3d12core,dxgi,d3d11,d3d10core=n,b"

        with patch.dict(os.environ, {"WINEDLLOVERRIDES": "winemenubuilder.exe=d"}):
            argv, env = prepared_wine_launch(
                ProtonInstall("Fixture", proton), exe, ["run", "Z:/title"], prefix,
                [("WINEDLLOVERRIDES", override)],
            )

        self.assertEqual(argv, [str(wine), str(exe), "run", "Z:/title"])
        self.assertEqual(env["WINEPREFIX"], str(prefix))
        self.assertEqual(env["WINEDLLOVERRIDES"], override)
