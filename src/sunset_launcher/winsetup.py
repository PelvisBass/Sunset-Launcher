"""Find and install the Windows tools that layer builds need: Git, Rust and the MSVC C++ build tools."""

from __future__ import annotations

import base64
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .backend import Runner

VC_TOOLS = "Microsoft.VisualStudio.Component.VC.Tools.x86.x64"
# Desktop development with C++. Its recommended components include the MSVC x64 tools and a Windows SDK.
VC_WORKLOAD = ("--add", "Microsoft.VisualStudio.Workload.VCTools", "--includeRecommended")
WINGET_FLAGS = (
    "--exact", "--source", "winget", "--accept-package-agreements", "--accept-source-agreements",
    "--disable-interactivity",
)
WINGET_IDS = {"git": "Git.Git", "rust": "Rustlang.Rustup", "msvc": "Microsoft.VisualStudio.BuildTools"}
MANUAL_URLS = {
    "git": "https://git-scm.com/downloads/win",
    "rust": "https://rustup.rs/",
    "msvc": "https://visualstudio.microsoft.com/visual-cpp-build-tools/",
}
RUST_HOST = "stable-x86_64-pc-windows-msvc"
# winget redraws spinners and progress bars, which arrive here as separate lines.
_PROGRESS = re.compile(r"\s*(?:[-\\|/]|.*[█▒].*|[\d.]+\s*[KMG]?B\s*/\s*[\d.]+\s*[KMG]?B|\d{1,3}%)\s*")

Run = Callable[..., subprocess.CompletedProcess]


@dataclass(frozen=True)
class BuildTool:
    key: str
    name: str
    size: str
    found: bool
    detail: str = ""
    # A Visual Studio installation that lacks the C++ tools, to add them to instead of installing another.
    install_path: str = ""


def _get(env: Mapping[str, str], name: str) -> str:
    """Windows environment names ignore case, and a copy of os.environ there has them upper-cased."""
    if name in env:
        return env[name]
    wanted = name.upper()
    return next((value for key, value in env.items() if key.upper() == wanted), "")


def tool_folders(env: Mapping[str, str]) -> list[str]:
    """Default Git, Rust and winget folders. Tools installed after the launcher started are missing from its PATH."""
    folders = []
    cargo = _get(env, "CARGO_HOME") or (os.path.join(_get(env, "USERPROFILE"), ".cargo") if _get(env, "USERPROFILE") else "")
    if cargo:
        folders.append(os.path.join(cargo, "bin"))
    for name in ("ProgramFiles", "ProgramW6432"):
        if root := _get(env, name):
            folders.append(os.path.join(root, "Git", "cmd"))
    if local := _get(env, "LOCALAPPDATA"):
        folders.append(os.path.join(local, "Programs", "Git", "cmd"))
        folders.append(os.path.join(local, "Microsoft", "WindowsApps"))
    return [folder for folder in dict.fromkeys(folders) if os.path.isdir(folder)]


def with_tool_folders(env: Mapping[str, str]) -> dict[str, str]:
    result = dict(env)
    key = next((name for name in result if name.upper() == "PATH"), "PATH")
    paths = [path for path in result.get(key, "").split(os.pathsep) if path]
    known = {os.path.normcase(os.path.normpath(path)) for path in paths}
    added = [folder for folder in tool_folders(result) if os.path.normcase(os.path.normpath(folder)) not in known]
    if added:
        result[key] = os.pathsep.join([*paths, *added])
    return result


def _which(name: str, env: Mapping[str, str]) -> str | None:
    """A program on `env`'s PATH. Tests that stand in for Windows on another system skip shutil.which there,
    because it then calls Windows-only functions."""
    if os.name == "nt" or sys.platform != "win32":
        return shutil.which(name, path=_get(env, "PATH") or os.defpath)
    for folder in _get(env, "PATH").split(os.pathsep):
        candidate = os.path.join(folder, name) if folder else ""
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def _installer_folder(env: Mapping[str, str]) -> Path | None:
    root = _get(env, "ProgramFiles(x86)") or _get(env, "ProgramFiles")
    return Path(root, "Microsoft Visual Studio", "Installer") if root else None


def windows_sdk(env: Mapping[str, str]) -> bool:
    root = _get(env, "ProgramFiles(x86)") or _get(env, "ProgramFiles")
    return bool(root) and any(Path(root, "Windows Kits", "10", "Lib").glob("*/um/x64/kernel32.lib"))


def visual_studio(env: Mapping[str, str], run: Run = subprocess.run) -> tuple[list[str], list[str]]:
    """Visual Studio installations with the MSVC x64 tools, and every installation, newest first."""
    folder = _installer_folder(env)
    vswhere = folder / "vswhere.exe" if folder else None
    if vswhere is None or not vswhere.is_file():
        return [], []

    def query(*extra: str) -> list[str]:
        command = [str(vswhere), "-products", "*", "-prerelease", "-sort", "-nologo", "-utf8",
                   "-property", "installationPath", *extra]
        try:
            result = run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, subprocess.SubprocessError):
            return []
        return [line.strip() for line in result.stdout.splitlines() if line.strip()] if result.returncode == 0 else []

    return query("-requires", VC_TOOLS), query()


def detect(env: Mapping[str, str] | None = None, run: Run = subprocess.run) -> tuple[BuildTool, ...]:
    """The build tools in the order they install. MSVC goes before Rust, whose installer otherwise asks for it."""
    env = with_tool_folders(os.environ if env is None else env)
    with_tools, installs = visual_studio(env, run)
    sdk = windows_sdk(env)
    if with_tools and sdk:
        msvc_detail = "Installed"
    elif with_tools:
        msvc_detail = "The Windows SDK is missing"
    elif installs:
        msvc_detail = "Visual Studio is installed without the C++ tools"
    else:
        msvc_detail = "Not installed"
    git = _which("git", env)
    cargo = _which("cargo", env)
    return (
        BuildTool("git", "Git", "about 350 MB", git is not None, "Installed" if git else "Not installed"),
        BuildTool(
            "msvc", "Visual Studio C++ Build Tools", "about 7 GB, with the Windows SDK", bool(with_tools and sdk),
            msvc_detail, install_path=(with_tools or installs or [""])[0],
        ),
        BuildTool("rust", "Rust", "about 1.5 GB once a layer's toolchain downloads", cargo is not None,
                  "Installed" if cargo else "Not installed"),
    )


def missing(tools: tuple[BuildTool, ...]) -> tuple[BuildTool, ...]:
    return tuple(tool for tool in tools if not tool.found)


def winget(env: Mapping[str, str] | None = None) -> str | None:
    return _which("winget", with_tool_folders(os.environ if env is None else env))


def _quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def modify_command(installer: Path, install_path: str) -> tuple[str, ...]:
    """Add the C++ workload to an existing Visual Studio, which asks Windows for administrator rights."""
    arguments = subprocess.list2cmdline(["modify", "--installPath", install_path, *VC_WORKLOAD, "--passive", "--norestart"])
    script = (
        f"$p = Start-Process -FilePath {_quote(str(installer))} -ArgumentList {_quote(arguments)} "
        "-Verb RunAs -Wait -PassThru; exit $p.ExitCode"
    )
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return ("powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded)


def install_command(tool: BuildTool, winget_path: str, env: Mapping[str, str]) -> tuple[str, ...]:
    if tool.key == "msvc":
        folder = _installer_folder(env)
        if tool.install_path and folder is not None and (folder / "setup.exe").is_file():
            return modify_command(folder / "setup.exe", tool.install_path)
        override = subprocess.list2cmdline(["--passive", "--wait", "--norestart", *VC_WORKLOAD])
        return (winget_path, "install", "--id", WINGET_IDS["msvc"], *WINGET_FLAGS, "--override", override)
    return (winget_path, "install", "--id", WINGET_IDS[tool.key], *WINGET_FLAGS, "--silent")


def install(runner: Runner, env: Mapping[str, str] | None = None, run: Run = subprocess.run) -> None:
    """Install every missing tool with winget, then check again. winget's exit code is not trusted: it also
    fails when a package is already present, or succeeds before Visual Studio has finished."""
    from .backend import Cancelled, OperationError, SetupRequired

    env = with_tool_folders(os.environ if env is None else env)
    needed = missing(detect(env, run))
    if not needed:
        runner.output("Every build tool is already installed.\n")
        return
    winget_path = _which("winget", env)
    if winget_path is None:
        raise SetupRequired(
            "winget (App Installer) is not available. Install it from the Microsoft Store, or install "
            + ", ".join(tool.name for tool in needed) + " by hand."
        )
    cwd = Path(_get(env, "USERPROFILE") or Path.home())
    output = runner.output

    def quiet(message: str):
        kept = "".join(line for line in message.splitlines(keepends=True) if not _PROGRESS.fullmatch(line.rstrip("\r\n")))
        if kept.strip():
            output(kept)

    for tool in needed:
        runner.check_cancelled()
        output(f"\nInstalling {tool.name} ({tool.size})…\n")
        if tool.key == "msvc":
            output("Windows asks for administrator permission, then the Visual Studio Installer shows its progress.\n")
        runner.output = quiet
        try:
            runner.run(install_command(tool, winget_path, env), cwd, env=env)
        except Cancelled:
            raise
        except OperationError as error:
            output(f"{error} Checking whether {tool.name} is installed anyway.\n")
        finally:
            runner.output = output
        if tool.key == "rust":
            env = with_tool_folders(env)
            rustup = _which("rustup", env)
            if rustup is not None:
                try:
                    runner.run((rustup, "show", "active-toolchain"), cwd, env=env, capture=True)
                except Cancelled:
                    raise
                except OperationError:
                    output("Setting up the stable Rust toolchain…\n")
                    runner.run((rustup, "default", RUST_HOST), cwd, env=env)
    still = missing(detect(env, run))
    if still:
        raise OperationError(
            f"Still missing after setup: {', '.join(tool.name for tool in still)}. "
            "See the activity log, or install them by hand."
        )
    output("\nBuild tools are ready.\n")
