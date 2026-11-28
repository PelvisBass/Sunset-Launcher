"""Fetch single files out of a remote zip, and give layers Microsoft's compiler and NVIDIA's DLSS files."""

import hashlib
import io
import os
import sys
import threading
import unittest
import zipfile
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from sunset_launcher import downloads
from sunset_launcher.backend import Library, Runner
from sunset_launcher.catalog import PROJECTS
from sunset_launcher.downloads import Archive, Member
from sunset_launcher.proton import proton_game_path


def archive_bytes(files: dict[str, bytes]) -> bytes:
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return data.getvalue()


class Server:
    """Serves one file over HTTP, with range requests unless `ranges` is off."""

    def __init__(self, body: bytes, *, ranges: bool = True):
        self.requests: list[str] = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_HEAD(self):
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()

            def do_GET(self):
                wanted = self.headers.get("Range")
                owner.requests.append(wanted or "")
                if wanted and ranges:
                    first, last = map(int, wanted.removeprefix("bytes=").split("-"))
                    part = body[first:last + 1]
                    self.send_response(206)
                    self.send_header("Content-Range", f"bytes {first}-{last}/{len(body)}")
                else:
                    part = body
                    self.send_response(200)
                self.send_header("Content-Length", str(len(part)))
                self.end_headers()
                try:
                    self.wfile.write(part)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # A stopped download hangs up mid-answer.

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/sdk.zip"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class FetchTests(unittest.TestCase):
    def setUp(self):
        self.dll = os.urandom(3 << 20)  # Bigger than one read, so it takes several ranges
        self.files = {"bin/x64/layer.dll": self.dll, "bin/x64/other.dll": b"not wanted", "license.txt": b"terms"}
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.output: list[str] = []

    def serve(self, **options) -> Server:
        server = Server(archive_bytes(self.files), **options)
        self.addCleanup(server.close)
        return server

    def archive(self, url: str, sha256: str | None = None) -> Archive:
        return Archive("Test SDK", url, "test-sdk", "about 3 MB", (
            Member("bin/x64/layer.dll", "layer.dll", sha256 or hashlib.sha256(self.dll).hexdigest()),
            Member("license.txt", "license.txt", None),
            Member("missing-license.txt", "missing-license.txt", None),
        ))

    def test_only_the_named_files_are_fetched_and_checked(self):
        server = self.serve()
        archive = self.archive(server.url)
        self.assertIsNone(downloads.ready(archive, self.root))
        folder = downloads.fetch(archive, self.root, self.output.append)
        self.assertEqual(downloads.ready(archive, self.root), folder)
        self.assertEqual((folder / "layer.dll").read_bytes(), self.dll)
        self.assertEqual((folder / "license.txt").read_bytes(), b"terms")
        self.assertEqual(sorted(path.name for path in folder.iterdir()), ["layer.dll", "license.txt"])
        self.assertTrue(all(request.startswith("bytes=") for request in server.requests))
        self.assertIn("Downloading Test SDK (about 3 MB) from 127.0.0.1", "".join(self.output))
        # A second fetch finds the files and asks for nothing.
        count = len(server.requests)
        downloads.fetch(archive, self.root, self.output.append)
        self.assertEqual(len(server.requests), count)

    def test_a_file_that_does_not_match_its_checksum_is_not_kept(self):
        server = self.serve()
        with self.assertRaisesRegex(OSError, "did not match its expected SHA-256"):
            downloads.fetch(self.archive(server.url, "0" * 64), self.root, self.output.append)
        self.assertEqual(list((self.root / "test-sdk").glob("layer.dll*")), [])

    def test_a_server_without_ranges_is_refused(self):
        server = self.serve(ranges=False)
        with self.assertRaisesRegex(OSError, "whole file instead of part"):
            downloads.fetch(self.archive(server.url), self.root, self.output.append)

    def test_a_stop_ends_the_download(self):
        server = self.serve()

        class Stop(Exception):
            pass

        def check():
            if len(server.requests) > 2:
                raise Stop

        with self.assertRaises(Stop):
            downloads.fetch(self.archive(server.url), self.root, self.output.append, check=check)
        self.assertFalse((self.root / "test-sdk" / "layer.dll").exists())

    def test_place_leaves_a_file_that_is_already_there(self):
        folder = self.root / "test-sdk"
        folder.mkdir()
        (folder / "layer.dll").write_bytes(b"ours")
        beside = self.root / "build"
        beside.mkdir()
        archive = self.archive("http://unused.invalid/")
        self.assertTrue(downloads.place(archive, self.root, "layer.dll", beside))
        self.assertEqual((beside / "layer.dll").read_bytes(), b"ours")
        (beside / "layer.dll").write_bytes(b"the user's own copy")
        self.assertTrue(downloads.place(archive, self.root, "layer.dll", beside))
        self.assertEqual((beside / "layer.dll").read_bytes(), b"the user's own copy")
        self.assertFalse(downloads.place(archive, self.root, "absent.dll", beside))

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux certificate bundles")
    def test_a_bundled_openssl_falls_back_to_the_system_certificates(self):
        missing = ("/nonexistent/cert.pem", "/nonexistent/certs")
        paths = type("Paths", (), {"cafile": missing[0], "capath": missing[1]})()
        bundle = next((path for path in downloads._CA_BUNDLES if os.path.isfile(path)), None)
        if bundle is None:
            self.skipTest("No system certificate bundle here")
        with (
            patch.dict(os.environ, {"SSL_CERT_FILE": ""}),
            patch("sunset_launcher.downloads.ssl.get_default_verify_paths", return_value=paths),
            patch("ssl.SSLContext.load_verify_locations") as load,
        ):
            downloads.ssl_context()
        load.assert_called_once_with(cafile=bundle)


class LayerFileTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.library = Library(Path(directory.name))
        self.output: list[str] = []
        self.runner = Runner(self.output.append)

    def project(self, key):
        return next(project for project in PROJECTS if project.key == key)

    def fetched(self, archive):
        folder = downloads.folder(archive, self.library.downloads_root())
        folder.mkdir(parents=True)
        for member in archive.required():
            (folder / member.name).write_bytes(b"x")
        return folder

    @patch.object(sys, "platform", "linux")
    def test_proton_layers_load_microsofts_compiler_beside_the_artifact(self):
        project = self.project("cool_fox")
        recipe = project.recipes["linux"]
        self.fetched(downloads.D3DCOMPILER)
        beside = self.library._artifact(project, recipe).parent
        beside.mkdir(parents=True)
        env = self.library._layer_files(project, recipe, self.runner, {"WINEDLLOVERRIDES": "dxgi=n,b"})
        self.assertEqual(env["WINEDLLOVERRIDES"], "dxgi=n,b;d3dcompiler_47=n,b")
        self.assertTrue((beside / "d3dcompiler_47.dll").is_file())
        self.assertNotIn("VICTORY_STREAMLINE_DIR", env)  # Cool Fox has no DLSS files to name

    @patch.object(sys, "platform", "linux")
    def test_a_missing_compiler_is_fetched_once_and_a_failure_only_warns(self):
        project = self.project("victory")
        recipe = project.recipes["linux"]
        self.library._artifact(project, recipe).parent.mkdir(parents=True)
        with patch.object(downloads, "fetch", side_effect=OSError("offline")) as fetch:
            env = self.library._layer_files(project, recipe, self.runner, {})
        fetch.assert_called_once()
        self.assertEqual(fetch.call_args.args[0], downloads.D3DCOMPILER)
        self.assertNotIn("WINEDLLOVERRIDES", env)
        self.assertIn("Could not download Microsoft's D3DCompiler 47: offline", "".join(self.output))
        self.assertIn("Series S may not draw", "".join(self.output))

    @patch.object(sys, "platform", "win32")
    def test_windows_uses_its_own_compiler(self):
        project = self.project("victory")
        with patch.object(downloads, "fetch") as fetch:
            env = self.library._layer_files(project, project.recipes["windows"], self.runner, {})
        fetch.assert_not_called()
        self.assertEqual(env, {})

    def test_dlss_files_are_named_once_fetched(self):
        project = self.project("nvidia_rt")
        for host in ("windows", "linux"):
            recipe = replace(project.recipes[host], native_d3dcompiler=False)
            with patch.object(sys, "platform", "win32" if host == "windows" else "linux"):
                self.assertTrue(self.library.needs_dlss_files(project))
                self.assertNotIn("VICTORY_STREAMLINE_DIR", self.library._layer_files(project, recipe, self.runner, {}))
        folder = self.fetched(downloads.STREAMLINE)
        with patch.object(sys, "platform", "linux"):
            self.assertFalse(self.library.needs_dlss_files(project))
            env = self.library._layer_files(project, replace(project.recipes["linux"], native_d3dcompiler=False),
                                            self.runner, {})
        self.assertEqual(env["VICTORY_STREAMLINE_DIR"], proton_game_path(folder))
        self.assertFalse(self.library.needs_dlss_files(self.project("cool_fox")))

    @patch.object(sys, "platform", "linux")
    def test_dlss_files_come_only_with_consent(self):
        project = self.project("nvidia_rt")
        with patch.object(self.library, "_fetch", return_value=True) as fetch:
            self.library.fetch_layer_files(project, self.runner)
            self.assertEqual([call.args[0] for call in fetch.call_args_list], [downloads.D3DCOMPILER])
            fetch.reset_mock()
            self.library.fetch_layer_files(project, self.runner, dlss=True)
            self.assertEqual([call.args[0] for call in fetch.call_args_list], [downloads.D3DCOMPILER, downloads.STREAMLINE])

    @patch.object(sys, "platform", "linux")
    def test_launch_joins_the_users_overrides_after_the_launchers(self):
        from sunset_launcher.proton import ProtonInstall

        project = self.project("cool_fox")
        recipe = project.recipes["linux"]
        self.fetched(downloads.D3DCOMPILER)
        game = Path(self.library.data) / "Game.exe"
        game.touch()
        self.library._artifact(project, recipe).parent.mkdir(parents=True)
        proton = ProtonInstall("Test Proton", Path(self.library.data) / "proton")
        with (
            patch.object(self.library, "revision", return_value="fixture"),
            patch("sunset_launcher.backend.proton_launch", side_effect=lambda proton, exe, args, *a, **k: ([str(exe), *args], {})),
            patch.object(self.runner, "run") as run,
        ):
            self.library.launch(project, str(game), proton, self.runner, options="WINEDLLOVERRIDES=winmm=n,b")
            env = run.call_args.kwargs["env"]
            self.assertEqual(env["WINEDLLOVERRIDES"], "d3dcompiler_47=n,b;winmm=n,b")
            # A driver pipeline cache of the layer's own, which nothing else pushes pipelines out of.
            gpu = self.library.project_dir(project) / "runtime/gpu-cache"
            self.assertEqual(env["__GL_SHADER_DISK_CACHE_PATH"], str(gpu / "nvidia"))
            self.assertEqual(env["__GL_SHADER_DISK_CACHE_SKIP_CLEANUP"], "1")
            self.assertEqual(env["MESA_SHADER_CACHE_DIR"], str(gpu / "mesa"))
            self.assertTrue((gpu / "nvidia").is_dir())
            self.library.warm_up(project, str(game), proton, self.runner, options="WINEDLLOVERRIDES=winmm=n,b")
            env = run.call_args.kwargs["env"]
            self.assertEqual(env["WINEDLLOVERRIDES"],
                             "d3d12,d3d12core,dxgi,d3d11,d3d10core,d3d9=n,b;d3dcompiler_47=n,b;winmm=n,b")

    @patch.object(sys, "platform", "linux")
    def test_rt_fork_under_proton_is_not_offered_windows_10s_direct3d_runtime(self):
        from sunset_launcher.proton import ProtonInstall

        project = self.project("nvidia_rt")
        game = Path(self.library.data) / "Game.exe"
        game.touch()
        proton = ProtonInstall("Test Proton", Path(self.library.data) / "proton")
        with (
            patch.object(self.library, "revision", return_value="fixture"),
            patch.object(downloads, "fetch", side_effect=OSError("offline")),
            patch("sunset_launcher.backend.proton_launch", side_effect=lambda proton, exe, args, *a, **k: ([str(exe), *args], {})),
            patch.object(self.runner, "run") as run,
        ):
            self.library.launch(project, str(game), proton, self.runner)
        command, env = run.call_args.args[0], run.call_args.kwargs["env"]
        # `play` reads it before its --set settings apply, so it stays in the environment.
        self.assertEqual(env["VICTORY_WINDOWS_BUILD"], "22631")
        # Streamline's own messages are left out of the log: under Proton they are an error every frame.
        self.assertEqual(env["VICTORY_LOG"], "info,streamline=off")
        self.assertNotIn("VICTORY_WINDOWS_BUILD=22631", command)
        self.assertNotIn("VICTORY_WINDOWS_BUILD", dict(project.recipes["windows"].run_env))


class ConsentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication

        from sunset_launcher.__main__ import apply_theme

        cls.app = QApplication.instance() or QApplication(["sunset-launcher-tests", "-platform", "offscreen"])
        apply_theme(cls.app)

    def test_nvidia_files_are_asked_for_once_and_remembered(self):
        from PySide6.QtWidgets import QMessageBox

        from sunset_launcher.ui import MainWindow

        asked = []

        def answer(choice):
            def exec_(box):
                asked.append(box.text())
                return 0

            def clicked(box):
                return next(button for button in box.buttons() if button.text() == choice)
            return exec_, clicked

        project = next(project for project in PROJECTS if project.key == "nvidia_rt")
        with (
            TemporaryDirectory() as directory,
            patch.object(sys, "platform", "linux"),
            patch.object(Library, "revision", lambda library, item: "a" * 40),
        ):
            window = MainWindow(Path(directory), scan_runtime=False, check_updates_on_startup=False)
            self.addCleanup(window.close)
            for choice, expected in (("Not now", False), ("Download", True), (None, True)):
                exec_, clicked = answer(choice or "Download")
                with (
                    patch.object(QMessageBox, "exec", exec_),
                    patch.object(QMessageBox, "clickedButton", clicked),
                    patch.object(window, "_begin_job") as begin,
                ):
                    window._start((project,), "update")
                self.assertEqual(begin.call_args.args[0].dlss, expected, choice)
            # Asked twice: "Not now" asks again next time, "Download" is remembered.
            self.assertEqual(asked, ["Download NVIDIA's DLSS files?"] * 2)
            with patch.object(Library, "needs_dlss_files", return_value=False), patch.object(window, "_begin_job") as begin:
                window._start((project,), "update")
            self.assertFalse(begin.call_args.args[0].dlss)


if __name__ == "__main__":
    unittest.main()
