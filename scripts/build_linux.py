"""Build the click-to-run x86-64 Linux release folder."""

from __future__ import annotations

import argparse
import platform
import shutil
import subprocess
import sys
import sysconfig
from importlib.metadata import distribution
from importlib.util import find_spec
from pathlib import Path
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]
PACKAGING = ROOT / "packaging"
APP_NAME = "SunsetLauncher"


def write_notices(bundle: Path) -> None:
    from PySide6.QtCore import qVersion

    licenses = bundle / "licenses"
    shutil.copyfile(ROOT / "LICENSE", bundle / "LICENSE")
    shutil.copytree(PACKAGING / "licenses", licenses)
    versions = {}
    for name in ("PySide6-Essentials", "shiboken6", "PyInstaller"):
        info = distribution(name)
        versions[name] = info.version
        for path in info.files or ():
            if ".dist-info" in path.parts[0] and path.name.upper().startswith(("LICENSE", "COPYING", "NOTICE")):
                shutil.copyfile(info.locate_file(path), licenses / f"{name}-{path.name}")
    python_version = platform.python_version()
    stdlib = Path(sysconfig.get_path("stdlib"))
    python_series = ".".join(python_version.split(".")[:2])
    for path in (
        stdlib / "LICENSE.txt",
        Path(sys.base_prefix) / "LICENSE.txt",
        Path(f"/usr/share/doc/libpython{python_series}-stdlib/copyright"),
    ):
        if path.is_file():
            shutil.copyfile(path, licenses / "CPython-LICENSE.txt")
            break

    qt_version = qVersion()
    qt_series = ".".join(qt_version.split(".")[:2])
    binding_version = versions["PySide6-Essentials"]
    (bundle / "THIRD_PARTY_NOTICES.txt").write_text(
        f"""Sunset Launcher uses the following third-party components.

Qt {qt_version}, PySide6 {binding_version}, and Shiboken6 {versions["shiboken6"]}
Copyright The Qt Company Ltd. and respective contributors.
The bundled libraries are dynamically loaded. Their LGPLv3/GPLv3 license
texts are in the licenses folder. Compatible Qt and PySide6 libraries can
be substituted in _internal. These components retain their own license terms.

Qt source: https://download.qt.io/archive/qt/{qt_series}/{qt_version}/
Qt for Python source: https://download.qt.io/official_releases/QtForPython/pyside6/PySide6-{binding_version}-src/
Qt third-party notices: https://doc.qt.io/qt-6/licenses-used-in-qt.html
Qt for Python third-party notices: https://doc.qt.io/qtforpython-6/licenses.html

CPython {python_version}
Copyright Python Software Foundation and respective contributors.
Source and license: https://github.com/python/cpython/tree/v{python_version}

PyInstaller bootloader {versions["PyInstaller"]}
The GPL license and its bootloader exception are in the licenses folder.
Source: https://github.com/pyinstaller/pyinstaller/tree/v{versions["PyInstaller"]}

Miami Sunset (2019), photograph by Joe deSousa
Dedicated to the public domain under CC0 1.0 Universal.
Resized for distribution. The launcher crops and shades the display.
Source: https://commons.wikimedia.org/wiki/File:Miami_Sunset_(45863661094).jpg
CC0 deed: https://creativecommons.org/publicdomain/zero/1.0/
""",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir", type=Path, default=ROOT / "dist",
        help="folder in which to write SunsetLauncher (default: dist/ at the project root)",
    )
    arguments = parser.parse_args()
    if sys.platform != "linux" or platform.machine().lower() not in {"x86_64", "amd64"}:
        parser.error("this bundle must be built on x86-64 Linux")
    if find_spec("PyInstaller") is None:
        parser.error("install the bundle extra with uv run --extra bundle python scripts/build_linux.py")

    output = arguments.output_dir.resolve()
    with TemporaryDirectory(prefix="sunset-pyinstaller-") as temporary:
        work = Path(temporary)
        subprocess.run(
            [
                sys.executable, "-m", "PyInstaller",
                "--noconfirm",
                "--distpath", str(output),
                "--workpath", str(work),
                str(PACKAGING / "SunsetLauncher.spec"),
            ],
            cwd=ROOT,
            check=True,
        )
    bundle = output / APP_NAME
    write_notices(bundle)
    print(f"Click-to-run executable: {bundle / APP_NAME}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
