"""Keep the two most recent launcher job logs."""

import re
import stat
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from uuid import uuid4


_LOG_NAME = re.compile(r"[0-9]{8}-[0-9]{6}-[0-9a-f]{6}\.log")


def _check_directory(directory: Path) -> None:
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        raise OSError(f"The log directory must be a local folder: {directory}")


def prune_run_logs(directory: Path, *, current: Path | None = None) -> None:
    try:
        _check_directory(directory)
    except FileNotFoundError:
        return

    previous = []
    for path in directory.iterdir():
        if path == current or _LOG_NAME.fullmatch(path.name) is None:
            continue
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISREG(info.st_mode) and not getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            previous.append((info.st_mtime_ns, path))
    previous.sort(reverse=True)
    keep = 1 if current is not None else 2
    for _, path in previous[keep:]:
        path.unlink(missing_ok=True)


@contextmanager
def open_run_log(directory: Path):
    directory.mkdir(parents=True, exist_ok=True)
    _check_directory(directory)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = directory / f"{timestamp}-{uuid4().hex[:6]}.log"
    with path.open("x", encoding="utf-8", buffering=1) as stream:
        yield path, stream
