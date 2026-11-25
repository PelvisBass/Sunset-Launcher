"""Git, build and launch operations independent of the desktop interface."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from threading import Event, Lock, Timer

from .catalog import Project, Recipe
from .proton import ProtonInstall, prepared_wine_launch, proton_game_path, proton_launch
from .shader_cache import cache_files, configure_shader_cache, remove_cache_file


class OperationError(RuntimeError):
    pass


class SetupRequired(OperationError):
    pass


class Cancelled(OperationError):
    pass


@dataclass(frozen=True)
class UpdateCheck:
    revision: str
    status: str


def host_environment(env: dict[str, str] | None = None) -> dict[str, str]:
    """Keep the frozen Qt and Python libraries out of external tools' environment."""
    result = dict(os.environ if env is None else env)
    if not (getattr(sys, "frozen", False) and sys.platform == "linux"):
        return result
    original = result.pop("LD_LIBRARY_PATH_ORIG", None)
    if original is None:
        result.pop("LD_LIBRARY_PATH", None)
    else:
        result["LD_LIBRARY_PATH"] = original

    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        for name in ("QT_PLUGIN_PATH", "QML2_IMPORT_PATH", "QML_IMPORT_PATH"):
            paths = result.get(name, "").split(os.pathsep)
            external = [path for path in paths if path and not Path(path).is_relative_to(bundle)]
            if external:
                result[name] = os.pathsep.join(external)
            else:
                result.pop(name, None)

    paths = result.get("PATH", os.defpath).split(os.pathsep)
    cargo_home = Path(result.get("CARGO_HOME") or Path.home() / ".cargo").expanduser()
    for directory in (cargo_home / "bin", Path.home() / ".local/bin"):
        if directory.is_dir() and str(directory) not in paths:
            paths.append(str(directory))
    result["PATH"] = os.pathsep.join(paths)
    return result


def host_python() -> str | None:
    if not getattr(sys, "frozen", False):
        return sys.executable
    path = host_environment().get("PATH")
    return shutil.which("python3", path=path) or shutil.which("python", path=path)


def expand_command(command: Sequence[str], values: dict[str, str]) -> list[str]:
    try:
        return [part.format_map(values) for part in command]
    except KeyError as error:
        if error.args == ("python",):
            raise SetupRequired("Install system Python 3.10 or newer with venv support for this project.") from error
        raise


class Runner:
    def __init__(self, output: Callable[[str], None]):
        self.output = output
        self.stopped = Event()
        self._lock = Lock()
        self._process: subprocess.Popen | None = None
        self._kill_timer: Timer | None = None

    def check_cancelled(self) -> None:
        if self.stopped.is_set():
            raise Cancelled("Stopped by user.")

    def cancel(self) -> None:
        self.stopped.set()
        with self._lock:
            if self._process is not None:
                self._stop(self._process)

    def _stop(self, process: subprocess.Popen) -> None:
        if self._kill_timer is not None:
            return
        if os.name == "nt":
            try:
                subprocess.Popen(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW,
                )
            except OSError:
                process.kill()
            return
        self._signal_group(process.pid, signal.SIGINT)
        self._kill_timer = Timer(4, self._signal_group, args=(process.pid, signal.SIGKILL))
        self._kill_timer.daemon = True
        self._kill_timer.start()

    @staticmethod
    def _signal_group(pid: int, sig: int) -> None:
        try:
            os.killpg(pid, sig)
        except ProcessLookupError:
            pass

    def run(
        self,
        command: Sequence[str],
        cwd: Path,
        *,
        env: dict[str, str] | None = None,
        capture: bool = False,
        timeout: float | None = None,
    ) -> str:
        self.check_cancelled()
        display = subprocess.list2cmdline(command) if os.name == "nt" else shlex.join(command)
        self.output(f"$ {display}\n")
        options = (
            {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW}
            if os.name == "nt" else {"start_new_session": True}
        )
        try:
            process = subprocess.Popen(
                command, cwd=cwd, env=host_environment(env), stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", **options,
            )
        except OSError as error:
            raise OperationError(f"Could not start {command[0]}: {error}") from error
        with self._lock:
            self._process = process
            if self.stopped.is_set():
                self._stop(process)
        timed_out = False

        def expire() -> None:
            nonlocal timed_out
            with self._lock:
                if self._process is process:
                    timed_out = True
                    self._stop(process)

        timeout_timer = None
        if timeout is not None:
            timeout_timer = Timer(timeout, expire)
            timeout_timer.daemon = True
            timeout_timer.start()
        captured = []
        try:
            assert process.stdout is not None
            with process.stdout:
                for line in process.stdout:
                    self.output(line)
                    if capture:
                        captured.append(line)
            code = process.wait()
        finally:
            with self._lock:
                self._process = None
                if timeout_timer is not None:
                    timeout_timer.cancel()
                if self._kill_timer is not None:
                    self._kill_timer.cancel()
                    self._kill_timer = None
        self.check_cancelled()
        if timed_out:
            raise OperationError(f"{Path(command[0]).name} timed out after {timeout:g} seconds.")
        if code:
            raise OperationError(f"{Path(command[0]).name} exited with code {code}. See the activity log for details.")
        return "".join(captured).strip()


def recipe_for(project: Project) -> Recipe | None:
    platform = "windows" if sys.platform == "win32" else sys.platform
    return project.recipes.get(platform)


class Library:
    def __init__(self, data: Path):
        self.data = data

    def project_dir(self, project: Project) -> Path:
        return self.data / "projects" / project.key

    def source(self, project: Project) -> Path:
        return self.project_dir(project) / "source"

    def prefix(self, project: Project) -> Path:
        return self.project_dir(project) / "compatdata"

    def _record_path(self, project: Project) -> Path:
        return self.project_dir(project) / "build.json"

    def _artifact(self, project: Project, recipe: Recipe) -> Path:
        return self.source(project) / recipe.artifact

    def has_build_record(self, project: Project) -> bool:
        return self._record_path(project).is_file()

    def _build_file(self, path: Path) -> os.stat_result | None:
        """Check the file and each launcher-managed parent without following links."""
        parts = path.relative_to(self.data).parts
        if not parts or ".." in parts:
            return None
        current = self.data
        try:
            for part in parts:
                parent = current.lstat()
                if not stat.S_ISDIR(parent.st_mode) or getattr(parent, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                    return None
                current /= part
            info = current.lstat()
        except FileNotFoundError:
            return None
        if stat.S_ISREG(info.st_mode) and not getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            return info
        return None

    @staticmethod
    def _digest(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    def delete_build(self, project: Project, runner: Runner) -> None:
        recipe = self._require_recipe(project)
        record = self._record_path(project)
        recorded_at = self._build_file(record)
        if recorded_at is None:
            raise OperationError("No safe build record was found for this project.")
        info = json.loads(record.read_text(encoding="utf-8"))
        if not isinstance(info, dict) or info.get("artifact") != recipe.artifact or not isinstance(info.get("revision"), str):
            raise OperationError("The build record does not match this project's recipe.")
        outputs = info.get("outputs")
        if "outputs" in info and (not isinstance(outputs, dict) or not isinstance(outputs.get(recipe.artifact), str)):
            raise OperationError("The build record has an invalid output list.")

        # Older records identify only the executable. Keep files changed since that build.
        names = (recipe.artifact,) if outputs is None else (recipe.artifact, *recipe.companion_outputs)
        for name in names:
            if outputs is not None and name not in outputs:
                continue
            runner.check_cancelled()
            path = self.source(project) / name
            file = self._build_file(path)
            if file is None:
                runner.output(f"Skipped missing or linked output {name}.\n")
                continue
            unchanged = (
                max(file.st_mtime_ns, file.st_ctime_ns) <= recorded_at.st_mtime_ns
                if outputs is None else self._digest(path) == outputs[name]
            )
            current = self._build_file(path)
            if not unchanged or current is None or (current.st_mtime_ns, current.st_ctime_ns, current.st_ino) != (
                file.st_mtime_ns, file.st_ctime_ns, file.st_ino,
            ):
                runner.output(f"Kept changed output {name}.\n")
                continue
            path.unlink()
            runner.output(f"Deleted {name}.\n")
        record.unlink()

    def revision(self, project: Project) -> str | None:
        recipe = recipe_for(project)
        if recipe is None or not self._artifact(project, recipe).is_file():
            return None
        try:
            info = json.loads(self._record_path(project).read_text(encoding="utf-8"))
            revision = info["revision"]
            if info["artifact"] == recipe.artifact and isinstance(revision, str):
                return revision
        except (OSError, ValueError, TypeError, KeyError):
            pass
        return None

    def check_update(self, project: Project, runner: Runner) -> UpdateCheck:
        built = self.revision(project)
        if built is None:
            raise OperationError("Build this project before checking for updates.")
        source = self.source(project)
        if not (source / ".git").exists():
            raise OperationError("The project's Git checkout could not be found.")
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_NO_LAZY_FETCH": "1"}
        if not shutil.which("git", path=host_environment(env).get("PATH")):
            raise SetupRequired("Install Git to check for updates.")

        def git(*args: str, timeout: float | None = None) -> str:
            return runner.run(("git", *args), source, env=env, capture=True, timeout=timeout)

        origin = git("config", "--get", "remote.origin.url")
        if not self._matches_origin(origin, project.repository):
            raise OperationError(f"The checkout's origin differs from the catalog: {origin}")
        try:
            branch = git("symbolic-ref", "--quiet", "HEAD")
        except Cancelled:
            raise
        except OperationError as error:
            raise OperationError("Switch the checkout to an upstream-tracking branch and try again.") from error
        tracking = git("for-each-ref", "--format=%(upstream:remotename)%09%(upstream:remoteref)", branch)
        remote_name, _, ref = tracking.partition("\t")
        if remote_name != "origin" or not ref.startswith("refs/heads/"):
            raise OperationError("The current branch does not track a branch on origin.")

        output = git("ls-remote", "--quiet", "--exit-code", "--refs", "--", "origin", ref, timeout=15)
        revision = next((sha for sha, _, name in (line.partition("\t") for line in output.splitlines()) if name == ref), "")
        if not re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", revision):
            raise OperationError("Git did not report a commit for the tracked branch.")
        if built == revision:
            return UpdateCheck(revision, "Up to date")

        # An unseen tip cannot be classified locally without fetching Git objects.
        try:
            git("rev-parse", "--verify", "--quiet", f"{revision}^{{commit}}")
        except Cancelled:
            raise
        except OperationError:
            return UpdateCheck(revision, "Update available")
        ahead, behind = map(int, git("rev-list", "--left-right", "--count", f"{built}...{revision}").split())
        status = "Upstream diverged" if ahead and behind else "Local build ahead" if ahead else "Update available"
        return UpdateCheck(revision, status)

    @staticmethod
    def _matches_origin(origin: str, repository: str) -> bool:
        return origin.rstrip("/").removesuffix(".git").casefold() == repository.rstrip("/").removesuffix(".git").casefold()

    def _values(self, project: Project, recipe: Recipe) -> dict[str, str]:
        venv = self.project_dir(project) / "venv"
        venv_python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        values = {
            "repo": str(self.source(project)),
            "artifact": str(self._artifact(project, recipe)),
            "venv": str(venv),
            "venv_python": str(venv_python),
        }
        if python := host_python():
            values["python"] = python
        return values

    @staticmethod
    def _require_recipe(project: Project) -> Recipe:
        recipe = recipe_for(project)
        if recipe is None:
            raise OperationError(f"{project.name} has no recipe for this operating system.")
        return recipe

    def _build_environment(self) -> dict[str, str]:
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "CARGO_TERM_COLOR": "never"}
        if sys.platform != "linux":
            return env
        tools = self.data / "build-tools"
        cargo = tools / "cargo"
        sdk_cache = tools / "xwin"
        if (cargo / "bin/rustup").is_file():
            env.update(CARGO_HOME=str(cargo), RUSTUP_HOME=str(tools / "rustup"))
            # Use the repository's pin, or the launcher-local default.
            env.pop("RUSTUP_TOOLCHAIN", None)
        if sdk_cache.is_dir():
            env.setdefault("XWIN_CACHE_DIR", str(sdk_cache))
            env.setdefault("XWIN_ARCH", "x86_64")
        if (sdk_cache / "xwin/sdk/include/um/windows.h").is_file():
            env.setdefault("XWIN_SDK_ROOT", str(sdk_cache / "xwin"))
        extra = [directory for directory in (cargo / "bin", tools / "mingw/usr/bin", tools / "dxc/bin") if directory.is_dir()]
        if extra:
            env["PATH"] = os.pathsep.join([*map(str, extra), env.get("PATH", os.defpath)])
        return env

    def _check_tools(self, project: Project, recipe: Recipe, env: dict[str, str]) -> None:
        path = host_environment(env).get("PATH")
        missing = [tool for tool in ("git", *recipe.dependencies) if not shutil.which(tool, path=path)]
        if recipe.rust_target and not shutil.which(env.get("RUSTC", "rustc"), path=path):
            missing.append("rustc")
        if recipe.rust_target == "x86_64-pc-windows-gnu":
            key = "CARGO_TARGET_X86_64_PC_WINDOWS_GNU_LINKER"
            if key not in env and shutil.which("x86_64-w64-mingw32-gcc-posix", path=path):
                env[key] = "x86_64-w64-mingw32-gcc-posix"
            if not shutil.which(env.get(key, "x86_64-w64-mingw32-gcc"), path=path):
                missing.append("MinGW-w64 POSIX linker")
        if recipe.needs_dxc:
            dxc = env.get("DXC")
            staged = self.source(project) / "build/d3d12-smoke/toolchain/bin" / ("dxc.exe" if os.name == "nt" else "dxc")
            staged_available = staged.is_file() and os.access(staged, os.X_OK)
            if not shutil.which(os.path.expanduser(dxc or "dxc"), path=path) and (dxc or not staged_available):
                missing.append("DirectX Shader Compiler (dxc or DXC)")
        if recipe.needs_xwin_sdk:
            cache = Path(env.get("XDG_CACHE_HOME", Path.home() / ".cache"))
            sdk = Path(env.get("XWIN_SDK_ROOT", str(cache / "cargo-xwin/xwin"))).expanduser()
            if not (sdk / "sdk/include/um/windows.h").is_file():
                missing.append("Windows SDK/CRT cache (cargo xwin cache xwin --xwin-arch x86_64)")
        if missing:
            raise SetupRequired(f"Missing build prerequisites: {', '.join(missing)}. Open Build setup for instructions.")

    def build(self, project: Project, update: bool, runner: Runner) -> str:
        recipe = self._require_recipe(project)
        env = self._build_environment()
        self._check_tools(project, recipe, env)
        values = self._values(project, recipe)
        steps = [(step, expand_command(step.command, values)) for step in recipe.steps]

        source = self.source(project)
        source.parent.mkdir(parents=True, exist_ok=True)
        def git(*args: str, capture: bool = False) -> str:
            return runner.run(("git", *args), source, env=env, capture=capture)

        cloned = not source.exists()
        if cloned:
            runner.output("Cloning upstream source…\n")
            with tempfile.TemporaryDirectory(prefix=".clone-", dir=source.parent) as folder:
                staged = Path(folder) / "source"
                runner.run(("git", "clone", "--", project.repository, str(staged)), source.parent, env=env)
                runner.check_cancelled()
                staged.rename(source)
        elif not (source / ".git").exists():
            raise OperationError(f"The source folder is not a Git checkout: {source}")

        if update and not cloned:
            origin = git("config", "--get", "remote.origin.url", capture=True)
            if not self._matches_origin(origin, project.repository):
                raise OperationError(f"The checkout's origin differs from the catalog: {origin}")
            changed = git("status", "--porcelain", "--untracked-files=no", "--ignore-submodules=untracked", capture=True)
            if changed:
                raise OperationError("The checkout has local tracked changes. Commit or stash them before updating.")
            runner.output("Fetching upstream updates…\n")
            git("fetch")

        record = self._record_path(project)
        if update and not cloned:
            record.unlink(missing_ok=True)
            runner.output("Fast-forwarding the checkout…\n")
            git("merge", "--ff-only", "@{upstream}")
        if recipe.rust_target:
            rustc = env.get("RUSTC", "rustc")
            target_output = runner.run(
                (rustc, "--print", "target-libdir", "--target", recipe.rust_target),
                source, env=env, capture=True,
            ).splitlines()
            if not target_output or not any(Path(target_output[-1]).glob("libcore-*.rlib")):
                raise SetupRequired(
                    f"Rust target {recipe.rust_target} is missing. "
                    f"Add it to the active toolchain with rustup target add {recipe.rust_target}."
                )
        if (source / ".gitmodules").is_file():
            git("submodule", "update", "--init", "--recursive")

        companion_before = {
            name: self._build_file(source / name) for name in recipe.companion_outputs
        }
        record.unlink(missing_ok=True)
        artifact = self._artifact(project, recipe)
        artifact.unlink(missing_ok=True)
        for step, command in steps:
            runner.output(f"\n{step.label}\n")
            runner.run(command, Path(step.working_directory.format_map(values)), env=env)

        if self._build_file(artifact) is None:
            raise OperationError(f"The build did not produce {recipe.artifact}. The upstream build layout may have changed.")
        revision = git("rev-parse", "HEAD", capture=True)
        outputs = {recipe.artifact: self._digest(artifact)}
        for name, before in companion_before.items():
            path = source / name
            after = self._build_file(path)
            if after is not None and (before is None or after.st_mtime_ns != before.st_mtime_ns or after.st_ino != before.st_ino):
                outputs[name] = self._digest(path)
        runner.check_cancelled()
        temporary = record.with_suffix(".tmp")
        temporary.write_text(
            json.dumps({"revision": revision, "artifact": recipe.artifact, "outputs": outputs}), encoding="utf-8",
        )
        temporary.replace(record)
        return revision

    def shader_usage(self, project: Project, runner: Runner) -> int:
        recipe = self._require_recipe(project)
        return sum(info.st_size for _, info in cache_files(self.project_dir(project), recipe, runner.check_cancelled))

    def clear_shader_cache(self, project: Project, runner: Runner) -> int:
        recipe = self._require_recipe(project)
        total = 0
        removed = 0
        for path, info in cache_files(self.project_dir(project), recipe, runner.check_cancelled):
            runner.check_cancelled()
            if remove_cache_file(path, info):
                total += info.st_size
                removed += 1
        runner.output(f"Removed {removed:,} shader cache files ({total:,} bytes).\n")
        return self.shader_usage(project, runner)

    def game_path(self, project: Project, raw: str) -> Path:
        if not raw.strip():
            raise OperationError("Choose the game files before launching.")
        game = Path(raw).expanduser().resolve()
        if game.is_file():
            return game
        if game.is_dir():
            if project.game_input == "either":
                return game
            raise OperationError(f"Choose the game executable inside the shared game folder to launch {project.name}.")
        expected = "file or folder" if project.game_input == "either" else "file"
        raise OperationError(f"The selected {expected} could not be found: {game}")

    def launch(
        self, project: Project, raw_game: str, proton: ProtonInstall | None,
        runner: Runner, *, prepared_prefix: Path | None = None,
    ) -> None:
        recipe = self._require_recipe(project)
        if self.revision(project) is None:
            raise OperationError("Build this project successfully before launching.")
        game = self.game_path(project, raw_game)
        values = self._values(project, recipe)
        values["game"] = proton_game_path(game) if recipe.runtime == "proton" else str(game)
        command = expand_command(recipe.command, values)
        env = None
        if recipe.runtime == "proton":
            if proton is None:
                raise OperationError("Select an installed Proton version before launching.")
            if prepared_prefix is not None and recipe.prepared_wine_env:
                command, env = prepared_wine_launch(
                    proton, Path(command[0]), command[1:], prepared_prefix, recipe.prepared_wine_env,
                )
            else:
                command, env = proton_launch(
                    proton, Path(command[0]), command[1:], self.prefix(project), game_path=game,
                    tool_path=host_environment().get("PATH"),
                )
        if recipe.launch_paths:
            env = os.environ.copy() if env is None else env
            for variable, directory in recipe.launch_paths:
                path = self.project_dir(project) / "runtime" / directory
                env[variable] = proton_game_path(path) if recipe.runtime == "proton" else str(path)
        if recipe.shader_cache:
            env = os.environ.copy() if env is None else env
            configure_shader_cache(recipe, self.project_dir(project), env)
        runner.run(command, Path(recipe.working_directory.format_map(values)), env=env)
