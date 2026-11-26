# -*- mode: python ; coding: utf-8 -*-

import sys
from pathlib import Path

packaging = Path(SPECPATH)
source = packaging.parent / "src"
package = source / "sunset_launcher"

analysis = Analysis(
    [str(packaging / "entry.py")],
    pathex=[str(source)],
    datas=[
        (str(package / "style.qss"), "sunset_launcher"),
        (str(package / "assets" / "harbor.jpg"), "sunset_launcher/assets"),
        (str(package / "assets" / "chevron-down.png"), "sunset_launcher/assets"),
        (str(package / "assets" / "check.png"), "sunset_launcher/assets"),
    ],
)
if sys.platform == "linux":
    # Display libraries and graphics drivers must share the host C++ and X11
    # runtimes. PyInstaller keeps the embedded Python as an automatic exception.
    analysis.exclude_system_libraries()

archive = PYZ(analysis.pure)
executable = EXE(
    archive,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="SunsetLauncher",
    upx=False,
    # A Windows GUI build would otherwise open a console window beside the launcher.
    console=sys.platform != "win32",
)
bundle = COLLECT(
    executable,
    analysis.binaries,
    analysis.datas,
    upx=False,
    name="SunsetLauncher",
)
