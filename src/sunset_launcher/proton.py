"""Discover installed Linux Proton builds and prepare isolated launches."""

from __future__ import annotations

import os
import re
import shutil
import sys
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath


@dataclass(frozen=True)
class ProtonInstall:
    name: str
    executable: Path
    steam_root: Path | None = None
    source: str = "Manual"


_VDF_PAIR = re.compile(r'"([^"\r\n]+)"[ \t]+"((?:\\.|[^"\\\r\n])*)"')
# Official Steam Linux Runtime app IDs and conventional install directories.
_RUNTIMES = {
    "1391110": "SteamLinuxRuntime_soldier",
    "1628350": "SteamLinuxRuntime_sniper",
    "4183110": "SteamLinuxRuntime_4",
}


def proton_supported() -> bool:
    return sys.platform.startswith("linux")


def _vdf_pairs(path: Path) -> Iterator[tuple[str, str]]:
    try:
        contents = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return
    for line in contents.splitlines():
        if not line.lstrip().startswith("//"):
            for key, value in _VDF_PAIR.findall(line):
                yield key.casefold(), re.sub(r'\\([\\"])', r'\1', value)


def _vdf_value(path: Path, key: str) -> str | None:
    return next((value for field, value in _vdf_pairs(path) if field == key), None)


def _xdg_path(variable: str, default: Path) -> Path:
    value = Path(os.environ.get(variable, "")).expanduser()
    return value if value.is_absolute() else default


def _steam_roots() -> Iterator[tuple[Path, str]]:
    home = Path.home()
    data_home = _xdg_path("XDG_DATA_HOME", home / ".local/share")
    flatpak = home / ".var/app/com.valvesoftware.Steam"
    snap = home / "snap/steam/common"
    candidates: list[tuple[Path, str]] = [
        (data_home / "Steam", "Steam"),
        (home / ".local/share/Steam", "Steam"),
        (home / ".steam/root", "Steam"),
        (home / ".steam/steam", "Steam"),
        (home / ".steam/debian-installation", "Steam"),
        (flatpak / "data/Steam", "Steam Flatpak"),
        (flatpak / ".local/share/Steam", "Steam Flatpak"),
        (flatpak / ".steam/root", "Steam Flatpak"),
        (flatpak / ".steam/steam", "Steam Flatpak"),
        (snap / ".local/share/Steam", "Steam Snap"),
        (snap / ".steam/root", "Steam Snap"),
        (snap / ".steam/steam", "Steam Snap"),
    ]
    for variable in ("STEAM_COMPAT_CLIENT_INSTALL_PATH", "STEAMROOT"):
        if configured := os.environ.get(variable):
            candidates.insert(0, (Path(configured).expanduser(), "Steam"))

    seen: set[Path] = set()
    for root, source in candidates:
        if root.is_absolute() and root.is_dir():
            root = root.resolve()
            if root not in seen:
                seen.add(root)
                yield root, source


def _steam_libraries(root: Path) -> Iterator[Path]:
    yield root
    seen = {root}
    for manifest in (root / "steamapps/libraryfolders.vdf", root / "config/libraryfolders.vdf"):
        for key, value in _vdf_pairs(manifest):
            # Modern manifests use a path field. Legacy manifests use numeric keys.
            if key != "path" and not key.isdecimal():
                continue
            path = Path(value)
            if path.is_absolute() and path.is_dir():
                path = path.resolve()
                if path not in seen:
                    seen.add(path)
                    yield path


def _extra_tool_roots() -> Iterator[tuple[Path, str]]:
    home = Path.home()
    data_home = _xdg_path("XDG_DATA_HOME", home / ".local/share")
    config_home = _xdg_path("XDG_CONFIG_HOME", home / ".config")

    for path in os.environ.get("STEAM_EXTRA_COMPAT_TOOLS_PATHS", "").split(os.pathsep):
        if path:
            yield Path(path).expanduser(), "Steam custom tools"

    yield from (
        (data_home / "umu/compatibilitytools", "umu"),
        (data_home / "lutris/runners/wine", "Lutris"),
        (config_home / "heroic/tools/proton", "Heroic"),
        (home / ".var/app/com.heroicgameslauncher.hgl/config/heroic/tools/proton", "Heroic Flatpak"),
        (Path("/usr/share/steam/compatibilitytools.d"), "System"),
        (Path("/usr/local/share/steam/compatibilitytools.d"), "System"),
    )
    if umu_folders := os.environ.get("UMU_FOLDERS_PATH"):
        base = Path(umu_folders).expanduser()
        yield base / "umu/compatibilitytools", "umu"
        yield base / "Steam/compatibilitytools.d", "umu"


def _is_executable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def _tool_executables(directory: Path) -> Iterator[Path]:
    yield directory / "proton"
    try:
        entries = sorted(directory.iterdir())
    except OSError:
        return
    for entry in entries:
        if entry.is_dir():
            yield entry / "proton"
        elif entry.suffix == ".vdf":
            # Steam also accepts manifests that point to a separately installed tool.
            if install_path := _vdf_value(entry, "install_path"):
                yield entry.parent / install_path / "proton"


def _proton_name(executable: Path) -> str:
    name = _vdf_value(executable.parent / "compatibilitytool.vdf", "display_name")
    return name or (executable.parent.name if executable.name == "proton" else executable.name)


def discover_proton() -> list[ProtonInstall]:
    """List available Proton executables, deduplicated across Steam path aliases."""
    if not proton_supported():
        return []

    roots = list(_steam_roots())
    locations: list[tuple[Path, Path | None, str]] = []
    for root, source in roots:
        locations.append((root / "compatibilitytools.d", root, source))
        for library in _steam_libraries(root):
            locations.append((library / "steamapps/common", root, source))
    locations.extend((path, None, source) for path, source in _extra_tool_roots())

    installations: dict[Path, ProtonInstall] = {}
    for path, steam_root, source in locations:
        for executable in _tool_executables(path):
            if _is_executable(executable):
                executable = executable.resolve()
                if executable not in installations:
                    installations[executable] = ProtonInstall(
                        _proton_name(executable), executable, steam_root, source
                    )

    return sorted(installations.values(), key=lambda tool: (tool.name.casefold(), str(tool.executable)))


def custom_proton(path: str | Path) -> ProtonInstall:
    """Accept a Proton script or the directory containing it."""
    executable = Path(path).expanduser().absolute()
    if executable.is_dir():
        executable /= "proton"
    if not _is_executable(executable):
        raise ValueError(f"Select an executable Proton script or its installation folder: {executable}")
    executable = executable.resolve()
    for install in discover_proton():
        if install.executable == executable:
            return install
    return ProtonInstall(_proton_name(executable), executable)


def proton_game_path(path: Path) -> str:
    """Convert a host path to the default Windows Z: drive used by Proton."""
    absolute = path.expanduser().resolve()
    return str(PureWindowsPath("Z:/", *absolute.parts[1:]))


def prepared_wine_prefix(path: Path) -> Path:
    prefix = path.expanduser().resolve()
    if not (prefix / "system.reg").is_file() or not (prefix / "drive_c").is_dir():
        raise ValueError(f"Choose the prepared Wine prefix folder containing system.reg and drive_c: {prefix}")
    return prefix


def prepared_wine_launch(
    install: ProtonInstall, executable: Path, args: Iterable[str],
    prefix: Path, defaults: Iterable[tuple[str, str]],
) -> tuple[list[str], dict[str, str]]:
    """Use Proton's Wine with an existing prefix configured for a project."""
    prefix = prepared_wine_prefix(prefix)
    install_dir = install.executable.parent
    wine = next(
        (candidate for directory in ("files", "dist")
         if _is_executable(candidate := install_dir / directory / "bin/wine")),
        None,
    )
    if wine is None:
        raise ValueError(f"{install.name} does not expose a Wine executable. Use the Proton version from the launch guide.")
    env = os.environ.copy()
    for key, value in defaults:
        if key == "WINEDLLOVERRIDES":
            env[key] = value
        else:
            env.setdefault(key, value)
    env["WINEPREFIX"] = str(prefix)
    wineserver = wine.with_name("wineserver")
    if _is_executable(wineserver):
        env["WINESERVER"] = str(wineserver)
    return [str(wine), str(executable), *args], env


def _runtime_entry(app_id: str, libraries: Iterable[Path]) -> Path | None:
    for library in libraries:
        steamapps = library / "steamapps"
        installdir = _vdf_value(steamapps / f"appmanifest_{app_id}.acf", "installdir")
        for name in (installdir, _RUNTIMES.get(app_id)):
            if name and Path(name).name == name and name not in {".", ".."}:
                entry = steamapps / "common" / name / "_v2-entry-point"
                if _is_executable(entry):
                    return entry
    return None


def proton_launch(
    install: ProtonInstall,
    executable: Path,
    args: Iterable[str],
    prefix: Path,
    *,
    game_path: Path | None = None,
    tool_path: str | None = None,
    verb: str = "run",
) -> tuple[list[str], dict[str, str]]:
    """Return argv and environment for running a Windows build on Linux.

    `run` starts a game through Proton's steam.exe, which does not pass the console on.
    `runinprefix` runs the program directly in the same prefix, so its output reaches the launcher.
    """
    if not proton_supported():
        raise ValueError("Proton launches are available on Linux.")
    if not _is_executable(install.executable):
        raise ValueError(f"The selected Proton executable is unavailable: {install.executable}")

    prefix = prefix.expanduser().resolve()
    prefix.mkdir(parents=True, exist_ok=True)
    executable = executable.expanduser().resolve()
    proton_dir = install.executable.parent
    manifest = proton_dir / "toolmanifest.vdf"
    runtime_id = _vdf_value(manifest, "require_tool_appid")

    roots = [root for root, _source in _steam_roots()]
    if install.steam_root:
        roots.insert(0, install.steam_root)
    steam_root = roots[0] if roots else None
    libraries = list(dict.fromkeys(library for root in roots for library in _steam_libraries(root)))

    env = os.environ.copy()
    for key in ("WINEPREFIX", "PROTONPATH", "UMU_NO_PROTON", "UMU_NO_RUNTIME", "RUNTIMEPATH"):
        env.pop(key, None)
    env.update(
        STEAM_COMPAT_CLIENT_INSTALL_PATH=str(steam_root) if steam_root else "",
        STEAM_COMPAT_DATA_PATH=str(prefix),
        STEAM_COMPAT_SHADER_PATH=str(prefix / "shadercache"),
        STEAM_COMPAT_INSTALL_PATH=str(executable.parent),
        STEAM_COMPAT_LIBRARY_PATHS=os.pathsep.join(map(str, libraries)),
        STEAM_COMPAT_APP_ID="0",
        SteamAppId="0",
        SteamGameId="0",
    )
    if game_path is not None:
        game_path = game_path.expanduser().resolve()
        mount = game_path if game_path.is_dir() else game_path.parent
        existing = env.get("PRESSURE_VESSEL_FILESYSTEMS_RO", "")
        env["PRESSURE_VESSEL_FILESYSTEMS_RO"] = os.pathsep.join(filter(None, (existing, str(mount))))

    # umu recreates the Steam Linux Runtime launch environment outside Steam.
    umu = shutil.which("umu-run", path=tool_path)
    if umu and install.executable.name == "proton" and manifest.is_file():
        env.update(WINEPREFIX=str(prefix), PROTONPATH=str(proton_dir), GAMEID="umu-default", STORE="")
        env["PROTON_VERB"] = verb
        return [umu, str(executable), *args], env

    command = [str(install.executable), verb, str(executable), *args]
    tool_paths = [str(proton_dir)]
    if runtime_id and (runtime := _runtime_entry(runtime_id, libraries)):
        tool_paths.append(str(runtime.parent))
        command = [str(runtime), f"--verb={verb}", "--", *command]
    env["STEAM_COMPAT_TOOL_PATHS"] = os.pathsep.join(tool_paths)
    env["STEAM_COMPAT_MOUNTS"] = env["STEAM_COMPAT_TOOL_PATHS"]
    return command, env
