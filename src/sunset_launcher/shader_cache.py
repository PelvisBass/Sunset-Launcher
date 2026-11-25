"""Locate generated shader and pipeline caches without traversing linked folders."""

from __future__ import annotations

import os
import re
import stat
from errno import ELOOP, ENOENT, ENOTDIR
from collections.abc import Callable, Iterator
from pathlib import Path

from .catalog import Recipe
from .proton import proton_game_path


WINE_LOCAL_APP_DATA = Path("drive_c/users/steamuser/AppData/Local")
XCL_FILES = re.compile(
    r"(?:[a-f0-9]{16}\.dxil"
    r"|translated/v2/[a-f0-9]{16}/[a-f0-9]{16}_[A-Za-z]+_\d+_\d+_[01]\.bin"
    r"|driver/(?:gfx-[a-f0-9]{16}-[a-f0-9]+|[a-f0-9]{16}-[a-f0-9]+-[a-f0-9]+)\.pso)"
)
VICTORY_FILES = re.compile(
    r"(?:shaders/(?:[a-f0-9]{16}/)?[a-f0-9]{2}/[a-f0-9]{62}"
    r"|pipelines/(?:host-pipelines\.(?:bin|next|tmp)|(?:early|prebuilt)-pipelines\.txt))"
)
TUXBOX_FILES = re.compile(
    r"(?:by_source/[a-f0-9]{64}|by_(?:vs|gs)_ps/[a-f0-9]{64}_[a-f0-9]{64}"
    r"|(?:runtime|dxil)/[a-f0-9]{64}/[a-f0-9]{64}(?:_[a-f0-9]{64}|_rt\d+)?)\.(?:cso|sha256)"
)
VKD3D_FILES = re.compile(r"vkd3d-proton(?:\.[^/]+)?\.cache(?:\.write)?")
STEAM_FILES = re.compile(r"(?:[^/]+/)*[^/]+\.(?:dxvk-cache|foz|foz\.idx)")


def configure_shader_cache(recipe: Recipe, project_dir: Path, environment: dict[str, str]) -> None:
    """Use launcher-local storage unless the user selects their own cache."""
    cache = project_dir / "runtime/cache"
    if recipe.shader_cache == "xcl":
        variable, cache = "XCL_DXIL_CACHE", cache / "dxil"
    elif recipe.shader_cache == "victory":
        if environment.get("VICTORY_CONFIG") or (project_dir / "source/victory.toml").is_file():
            return
        variable = "VICTORY_CACHE_DIR"
    elif recipe.shader_cache == "tuxbox" and recipe.runtime == "managed":
        variable, cache = "VKD3D_SHADER_CACHE_PATH", cache / "vkd3d"
        if variable not in environment:
            cache.mkdir(parents=True, exist_ok=True)
    else:
        return
    if variable not in environment:
        environment[variable] = proton_game_path(cache) if recipe.runtime == "proton" else str(cache)


def _unlinked(info: os.stat_result) -> bool:
    return not stat.S_ISLNK(info.st_mode) and not getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT


def _directory(path: Path) -> bool:
    path = path.absolute()
    if ".." in path.parts:
        return False
    try:
        for parent in (*reversed(path.parents), path):
            info = parent.lstat()
            if not stat.S_ISDIR(info.st_mode) or not _unlinked(info):
                return False
    except FileNotFoundError:
        return False
    return True


def _locations(project_dir: Path, recipe: Recipe) -> Iterator[tuple[Path, re.Pattern[str]]]:
    runtime_cache = project_dir / "runtime/cache"
    prefix = project_dir / "compatdata"
    wine_data = (prefix / WINE_LOCAL_APP_DATA, prefix / "pfx" / WINE_LOCAL_APP_DATA)

    if recipe.shader_cache == "xcl":
        yield runtime_cache / "dxil", XCL_FILES
        if recipe.runtime == "proton":
            for root in wine_data:
                for folder in ("xcl", "XCL"):
                    yield root / folder / "dxil", XCL_FILES
    elif recipe.shader_cache == "victory":
        yield runtime_cache, VICTORY_FILES
        if recipe.runtime == "proton":
            for root in wine_data:
                yield root / "victory/cache", VICTORY_FILES
    elif recipe.shader_cache == "tuxbox":
        if recipe.runtime == "managed":
            yield runtime_cache / "vkd3d", VKD3D_FILES
            user_data = Path(os.environ.get("XDG_DATA_HOME", "")).expanduser()
            if not user_data.is_absolute():
                user_data = Path.home() / ".local/share"
            prefixes = user_data / "project-tuxbox/prefixes"
            if _directory(prefixes):
                for tuxbox_prefix in prefixes.iterdir():
                    yield tuxbox_prefix / WINE_LOCAL_APP_DATA / "Temp/tuxbox_shader_cache", TUXBOX_FILES

    if recipe.runtime == "proton":
        yield project_dir / "source", re.compile(r"vkd3d-proton\.cache(?:\.write)?")
        yield prefix / "shadercache", STEAM_FILES


def _valid_signature(path: Path, relative: str, pattern: re.Pattern[str], info: os.stat_result) -> bool:
    suffix = path.suffix
    if path.name.startswith("vkd3d-proton"):
        signatures = (b"VKS",)
    elif suffix in {".dxil", ".cso"}:
        signatures = (b"DXBC",)
    elif pattern is XCL_FILES and relative.startswith("translated/"):
        signatures = (b"XCLTRAN2",)
    elif pattern is VICTORY_FILES and relative.startswith("shaders/"):
        signatures = (b"VSH1", b"VSHE")
    elif suffix == ".sha256":
        signatures = ()
    else:
        return True
    try:
        with path.open("rb") as stream:
            if not os.path.samestat(os.fstat(stream.fileno()), info):
                return False
            if suffix == ".sha256":
                return re.fullmatch(rb"[a-f0-9]{64}\n?", stream.read(66)) is not None
            return stream.read(max(map(len, signatures))).startswith(signatures)
    except FileNotFoundError:
        return False


def cache_files(
    project_dir: Path, recipe: Recipe, check_cancelled: Callable[[], None],
) -> Iterator[tuple[Path, os.stat_result]]:
    seen: set[Path] = set()
    for root, pattern in _locations(project_dir, recipe):
        if not _directory(root):
            continue
        # The checkout only has cache files at its root. Its build tree may be large.
        shallow = root == project_dir / "source"
        pending = [root]
        while pending:
            check_cancelled()
            directory = pending.pop()
            with os.scandir(directory) as children:
                for child in children:
                    try:
                        info = child.stat(follow_symlinks=False)
                    except FileNotFoundError:
                        continue
                    if not _unlinked(info):
                        continue
                    path = Path(child.path)
                    if stat.S_ISDIR(info.st_mode) and not shallow:
                        pending.append(path)
                    if not stat.S_ISREG(info.st_mode) or path in seen:
                        continue
                    relative = path.relative_to(root).as_posix()
                    if not pattern.fullmatch(relative):
                        continue
                    # On Windows DirEntry can return zero for file identity fields.
                    if os.name == "nt":
                        try:
                            info = path.lstat()
                        except FileNotFoundError:
                            continue
                        if not stat.S_ISREG(info.st_mode) or not _unlinked(info):
                            continue
                    if _valid_signature(path, relative, pattern, info):
                        seen.add(path)
                        yield path, info


def remove_cache_file(path: Path, scanned: os.stat_result) -> bool:
    """Recheck link and file identity before unlinking a candidate."""
    descriptor = None
    try:
        if os.unlink in os.supports_dir_fd and hasattr(os, "O_NOFOLLOW") and hasattr(os, "O_DIRECTORY"):
            absolute = path.absolute()
            if ".." in absolute.parts:
                return False
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            descriptor = os.open(absolute.anchor, flags)
            for part in absolute.parent.parts[1:]:
                child = os.open(part, flags, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
            current = os.stat(absolute.name, dir_fd=descriptor, follow_symlinks=False)
        else:
            if not _directory(path.parent):
                return False
            current = path.lstat()
        if not stat.S_ISREG(current.st_mode) or not _unlinked(current):
            return False
        if (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns, current.st_ctime_ns) != (
            scanned.st_dev, scanned.st_ino, scanned.st_size, scanned.st_mtime_ns, scanned.st_ctime_ns,
        ):
            return False
        if descriptor is None:
            path.unlink()
        else:
            os.unlink(path.name, dir_fd=descriptor)
    except OSError as error:
        if error.errno in {ELOOP, ENOENT, ENOTDIR}:
            return False
        raise
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return True
