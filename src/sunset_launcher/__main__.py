"""Desktop entry point for Sunset Launcher."""

import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from PySide6.QtCore import QLockFile, QStandardPaths, QTimer
from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import QApplication, QMessageBox

from .ui import Hero, MainWindow


def apply_theme(app: QApplication) -> None:
    app.setStyle("Fusion")
    app.setFont(QFont("Sans Serif", 10))
    palette = QPalette()
    for role, color in {
        QPalette.ColorRole.Window: "#1b1522",
        QPalette.ColorRole.WindowText: "#f4edf7",
        QPalette.ColorRole.Base: "#19131f",
        QPalette.ColorRole.AlternateBase: "#211927",
        QPalette.ColorRole.Text: "#f4edf7",
        QPalette.ColorRole.Button: "#2a2032",
        QPalette.ColorRole.ButtonText: "#f4edf7",
        QPalette.ColorRole.Highlight: "#855277",
        QPalette.ColorRole.HighlightedText: "#fff3f9",
        QPalette.ColorRole.ToolTipBase: "#2a2032",
        QPalette.ColorRole.ToolTipText: "#f4edf7",
    }.items():
        palette.setColor(role, QColor(color))
    app.setPalette(palette)
    app.setStyleSheet(Path(__file__).with_name("style.qss").read_text(encoding="utf-8"))


def smoke_test(app: QApplication) -> int:
    with TemporaryDirectory(prefix="sunset-smoke-") as folder:
        window = MainWindow(Path(folder), scan_runtime=False)
        hero = window.findChild(Hero)
        if hero is None or hero.art.isNull() or not window.rows:
            print("Sunset Launcher smoke test failed to load the library or its artwork.", file=sys.stderr)
            return 1
        window.show()
        if window.grab().isNull():
            print("Sunset Launcher smoke test could not render the window.", file=sys.stderr)
            return 1
        QTimer.singleShot(0, window.close)
        code = app.exec()
    if code == 0:
        print("Sunset Launcher smoke test passed.")
    return code


def main() -> int:
    smoke = "--smoke-test" in sys.argv[1:]
    app = QApplication([argument for argument in sys.argv if argument != "--smoke-test"])
    app.setApplicationName("sunset-launcher")
    app.setApplicationDisplayName("Sunset Launcher")
    apply_theme(app)
    if smoke:
        return smoke_test(app)
    data = Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppLocalDataLocation))
    try:
        data.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        QMessageBox.critical(None, "Could not open Sunset Launcher", str(error))
        return 1
    lock = QLockFile(str(data / "launcher.lock"))
    lock.setStaleLockTime(0)
    if not lock.tryLock(0):
        QMessageBox.warning(None, "Sunset Launcher", "Another copy of the launcher is using this data folder.")
        return 1

    window = MainWindow(data)
    window.show()
    try:
        return app.exec()
    finally:
        lock.unlock()


if __name__ == "__main__":
    raise SystemExit(main())
