"""Exercise cache measurement and cleanup in isolated runtime directories."""

import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from sunset_launcher.backend import Library, Runner
from sunset_launcher.catalog import PROJECTS
from sunset_launcher.proton import proton_game_path
from sunset_launcher.shader_cache import WINE_LOCAL_APP_DATA, configure_shader_cache


class ShaderCacheTests(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        self.library = Library(self.root / "Launcher data")
        self.projects = {project.key: project for project in PROJECTS}
        self.runner = Runner(lambda _: None)

    def write(self, path: Path, contents: bytes = b"abcd") -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(contents)
        return path

    def test_victory_family_removes_only_recognized_cache_files(self):
        project = self.projects["ztrx"]
        cache = self.library.project_dir(project) / "runtime/cache"
        removed = [
            self.write(cache / "shaders/ab" / ("c" * 62), b"VSH1"),
            self.write(cache / "shaders" / ("b" * 16) / "af" / ("d" * 62), b"VSH1"),
            self.write(cache / "pipelines/host-pipelines.bin"),
            self.write(self.library.source(project) / "vkd3d-proton.cache", b"VKS\x04"),
        ]
        preserved = [
            self.write(cache / "images/module.dll"),
            self.write(cache / "shaders/notes.txt"),
            self.write(cache / "shaders/ad" / ("c" * 62), b"not a shader"),
            self.write(self.library.project_dir(project) / "runtime/data/save.bin"),
            self.write(self.library.project_dir(self.projects["victory"]) / "runtime/cache/shaders/ab" / ("f" * 62)),
            self.write(self.library.source(project) / "vkd3d-proton.cache.write", b"user file"),
        ]
        with patch.object(sys, "platform", "linux"):
            self.assertEqual(self.library.shader_usage(project, self.runner), 16)
            self.assertEqual(self.library.clear_shader_cache(project, self.runner), 0)
        self.assertTrue(all(not path.exists() for path in removed))
        self.assertTrue(all(path.is_file() for path in preserved))

    def test_xcl_legacy_cache_does_not_follow_symlinks(self):
        project = self.projects["xcl"]
        legacy = self.library.prefix(project) / WINE_LOCAL_APP_DATA / "xcl/dxil"
        shader = self.write(legacy / ("a" * 16 + ".dxil"), b"DXBC")
        translated = self.write(legacy / "translated/v2" / ("c" * 16) / ("d" * 16 + "_Mesh_0_1_0.bin"), b"XCLTRAN2")
        other = self.write(self.root / "outside" / ("b" * 16 + ".dxil"), b"DXBC")
        link = legacy / ("b" * 16 + ".dxil")
        try:
            link.symlink_to(other)
        except OSError as error:
            self.skipTest(f"Symlinks are unavailable: {error}")
        with patch.object(sys, "platform", "linux"):
            self.assertEqual(self.library.shader_usage(project, self.runner), 12)
            self.assertEqual(self.library.clear_shader_cache(project, self.runner), 0)
        self.assertFalse(shader.exists())
        self.assertFalse(translated.exists())
        self.assertTrue(other.exists())
        self.assertTrue(link.is_symlink())

    def test_tuxbox_preserves_the_prefix_and_uses_its_own_shader_subfolder(self):
        project = self.projects["tuxbox"]
        prefix = self.root / "project-tuxbox/prefixes/Proton - Experimental"
        cache = prefix / WINE_LOCAL_APP_DATA / "Temp/tuxbox_shader_cache"
        shader = self.write(cache / "by_source" / ("a" * 64 + ".cso"), b"DXBC")
        linked_shader = self.write(cache / "by_gs_ps" / ("a" * 64 + "_" + "b" * 64 + ".cso"), b"DXBC")
        save = self.write(prefix / "drive_c/users/steamuser/Saved Games/title/save.bin")
        notes = self.write(cache / "by_source/notes.txt")
        with patch.object(sys, "platform", "linux"), patch.dict(os.environ, {"XDG_DATA_HOME": str(self.root)}):
            self.assertEqual(self.library.shader_usage(project, self.runner), 8)
            self.assertEqual(self.library.clear_shader_cache(project, self.runner), 0)
        self.assertFalse(shader.exists())
        self.assertFalse(linked_shader.exists())
        self.assertTrue(save.is_file())
        self.assertTrue(notes.is_file())

    def test_launch_configures_isolated_cache_and_honors_explicit_overrides(self):
        project = self.projects["xcl"]
        path = self.library.project_dir(project)
        configure_shader_cache(project.recipes["windows"], path, native := {})
        self.assertEqual(native["XCL_DXIL_CACHE"], str(path / "runtime/cache/dxil"))
        if os.name != "nt":
            configure_shader_cache(project.recipes["linux"], path, proton := {})
            self.assertEqual(proton["XCL_DXIL_CACHE"], proton_game_path(path / "runtime/cache/dxil"))
        configure_shader_cache(project.recipes["windows"], path, customized := {"XCL_DXIL_CACHE": "custom"})
        self.assertEqual(customized["XCL_DXIL_CACHE"], "custom")
