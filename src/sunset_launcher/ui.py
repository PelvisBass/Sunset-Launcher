"""Desktop views and background job coordination."""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import threading
from contextlib import nullcontext
from os.path import expanduser, isdir, isfile
from pathlib import Path

from PySide6.QtCore import QDateTime, QLocale, QMimeData, QPoint, QRectF, QSettings, QThread, QTimer, Qt, QUrl, QObject, Signal, Slot
from PySide6.QtGui import QColor, QCloseEvent, QDesktopServices, QDrag, QIcon, QLinearGradient, QPainter, QPainterPath, QPixmap, QShowEvent, QTextCursor
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QCheckBox, QComboBox, QFileDialog, QFrame, QGridLayout, QHBoxLayout, QLabel,
    QLineEdit, QMainWindow, QMenu, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QScrollArea,
    QSizePolicy, QStackedWidget, QVBoxLayout, QWidget,
)

from . import RELEASES_URL, __version__
from .backend import (
    Cancelled, Library, OperationError, Runner, SetupRequired, WarmupProgress, host_environment,
    parse_launch_options, parse_warmup_progress, recipe_for,
)
from .catalog import PROJECTS, Project
from .logs import open_run_log, prune_run_logs
from .proton import ProtonInstall, custom_proton, discover_proton, prepared_wine_prefix, proton_supported
from .sysinfo import available_memory, process_tree_memory
from .warmup import WarmupTracker, format_bytes, format_duration

LIBRARY_PAGE, INSTALL_PAGE, WARMUP_PAGE, ACTIVITY_PAGE = range(4)
WARMUP_JOBS = (0, 2, 4, 8, 16)


def available_projects() -> tuple[Project, ...]:
    """Projects with a recipe for this operating system, or every project where none has one."""
    return tuple(project for project in PROJECTS if recipe_for(project) is not None) or PROJECTS


def _version(tag: str) -> tuple[int, ...] | None:
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", tag)
    return tuple(map(int, match.groups())) if match else None


def latest_release(ls_remote_output: str) -> str | None:
    """Return the newest vX.Y.Z tag from git ls-remote output when it is newer than this launcher."""
    tags = [line.rpartition("refs/tags/")[2] for line in ls_remote_output.splitlines() if "refs/tags/" in line]
    versions = [(version, tag) for tag in tags if (version := _version(tag)) is not None]
    if not versions:
        return None
    version, tag = max(versions)
    return tag if version > _version(__version__) else None


def fetch_latest_release() -> str | None:
    env = {**host_environment(), "GIT_TERMINAL_PROMPT": "0"}
    git = shutil.which("git", path=env.get("PATH"))
    if git is None:
        return None
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}
    try:
        result = subprocess.run(
            [git, "ls-remote", "--tags", "--refs", f"{RELEASES_URL}.git", "v*"],
            env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=20, **options,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return latest_release(result.stdout) if result.returncode == 0 else None


def label(value: str, style: str = "", *, wrap: bool = False) -> QLabel:
    widget = QLabel(value)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    widget.setObjectName(style)
    widget.setWordWrap(wrap)
    return widget


def button(value: str, *, quiet: bool = False, primary: bool = False) -> QPushButton:
    widget = QPushButton(value)
    widget.setProperty("quiet", quiet)
    if primary:
        widget.setObjectName("primary")
    widget.setCursor(Qt.CursorShape.PointingHandCursor)
    return widget


def rule() -> QFrame:
    line = QFrame()
    line.setProperty("rule", True)
    line.setFixedHeight(1)
    return line


def open_url(url: QUrl) -> bool:
    if getattr(sys, "frozen", False) and sys.platform == "linux":
        env = host_environment()
        if opener := shutil.which("xdg-open", path=env.get("PATH")):
            target = url.toLocalFile() if url.isLocalFile() else url.toString()
            try:
                subprocess.Popen(
                    [opener, target], env=env, start_new_session=True, stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
            except OSError:
                return False
            return True
    return QDesktopServices.openUrl(url)


def sun_logo() -> QPixmap:
    pixmap = QPixmap(80, 80)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    edge = QRectF(4, 4, 72, 72)
    circle = QPainterPath()
    circle.addEllipse(edge)
    painter.setClipPath(circle)
    gradient = QLinearGradient(20, 80, 56, 0)
    gradient.setColorAt(0, QColor("#c26cce"))
    gradient.setColorAt(0.5, QColor("#fa9ebd"))
    gradient.setColorAt(1, QColor("#ffc08a"))
    painter.fillRect(edge, gradient)
    for y, height in ((42, 3), (51, 4), (61, 5), (71, 6)):
        painter.fillRect(0, y, 80, height, QColor("#281b32"))
    painter.end()
    return pixmap


class Hero(QWidget):
    def __init__(self):
        super().__init__()
        self.art = QPixmap(str(Path(__file__).parent / "assets" / "harbor.jpg"))
        self.setFixedHeight(195)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(27, 14, 22, 22)
        layout.setSpacing(2)
        layout.addStretch()
        layout.addWidget(label("XBOX SERIES COMPATIBILITY", "eyebrow"))
        layout.addWidget(label("Build library", "heroTitle"))
        layout.addWidget(label("Build, update and launch your Xbox translation layers.", "heroText"))

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
        target = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        corners = QPainterPath()
        corners.addRoundedRect(target, 15, 15)
        painter.setClipPath(corners)
        painter.fillRect(target, QColor("#40253c"))
        if not self.art.isNull():
            scale = max(target.width() / self.art.width(), target.height() / self.art.height())
            source_width, source_height = target.width() / scale, target.height() / scale
            source = QRectF(
                (self.art.width() - source_width) / 2,
                (self.art.height() - source_height) * 0.63,
                source_width, source_height,
            )
            painter.drawPixmap(target, self.art, source)
        shade = QLinearGradient(target.topLeft(), target.topRight())
        shade.setColorAt(0, QColor(19, 12, 25, 215))
        shade.setColorAt(0.53, QColor(19, 12, 25, 105))
        shade.setColorAt(1, QColor(19, 12, 25, 14))
        painter.fillRect(target, shade)
        painter.setClipping(False)
        painter.setPen(QColor("#664159"))
        painter.drawPath(corners)


def initials_badge(project: Project) -> QLabel:
    initials = label(project.name[0].upper())
    initials.setAlignment(Qt.AlignmentFlag.AlignCenter)
    initials.setFixedSize(39, 39)
    initials.setStyleSheet(
        f"background: {QColor(53, 36, 61).name()}; color: {QColor(project.accent).name()}; border-radius: 9px;"
        "font-size: 19px; font-weight: 800;"
    )
    return initials


CONSOLE_LABELS = {"auto": "Automatic", "series-x": "Series X", "series-s": "Series S"}


def section(title: str) -> QLabel:
    heading = label(title.upper(), "section")
    heading.setContentsMargins(0, 6, 0, 0)
    return heading


class SegmentedControl(QWidget):
    """A row of exclusive buttons with the small part of QComboBox's interface the window uses."""

    activated = Signal(int)

    def __init__(self):
        super().__init__()
        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(0)
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._group.idClicked.connect(self.activated.emit)
        self._data: list[object] = []
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

    def addItem(self, text: str, data: object = None):
        segment = QPushButton(text)
        segment.setCheckable(True)
        segment.setProperty("segment", True)
        segment.setCursor(Qt.CursorShape.PointingHandCursor)
        self._group.addButton(segment, len(self._data))
        self._layout.addWidget(segment)
        self._data.append(data)
        self._shape()
        if len(self._data) == 1:
            segment.setChecked(True)
        # Segments added after the panel was laid out must apply their stylesheet size first,
        # and the panel must hear about it, or it keeps the shorter unstyled height.
        segment.ensurePolished()
        self.updateGeometry()

    def _shape(self):
        buttons = self._group.buttons()
        for index, segment in enumerate(buttons):
            position = "only" if len(buttons) == 1 else "first" if index == 0 else "last" if index == len(buttons) - 1 else "middle"
            segment.setProperty("position", position)
            segment.style().unpolish(segment)
            segment.style().polish(segment)

    def clear(self):
        for segment in self._group.buttons():
            self._group.removeButton(segment)
            # Take it out now: deleteLater alone would leave it in the row until the event loop runs.
            self._layout.removeWidget(segment)
            segment.setParent(None)
            segment.deleteLater()
        self._data.clear()
        self.updateGeometry()

    def count(self) -> int:
        return len(self._data)

    def currentIndex(self) -> int:
        return self._group.checkedId()

    def currentData(self):
        index = self.currentIndex()
        return self._data[index] if 0 <= index < len(self._data) else None

    def findData(self, data) -> int:
        return self._data.index(data) if data in self._data else -1

    def setCurrentIndex(self, index: int):
        segment = self._group.button(index)
        if segment is not None:
            segment.setChecked(True)


class NavButton(QPushButton):
    """A sidebar tab with a count or progress badge on the right."""

    def __init__(self, title: str):
        super().__init__()
        self.setCheckable(True)
        self.setProperty("nav", True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAccessibleName(title)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(13, 0, 11, 0)
        self.title = label(title, "navTitle")
        self.badge = label("", "navBadge")
        layout.addWidget(self.title, 1)
        layout.addWidget(self.badge)
        for child in (self.title, self.badge):
            child.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.toggled.connect(self._restyle)
        self._restyle(False)

    def _restyle(self, checked: bool):
        self.title.setStyleSheet(f"color: {'#ffb6d2' if checked else '#d1c0d9'}; font-weight: 650;")

    def set_badge(self, text: str):
        self.badge.setText(text)
        self.setAccessibleDescription(text)


class InstallCard(QFrame):
    def __init__(self, project: Project, runtime: str):
        super().__init__()
        self.project = project
        self.setObjectName("installCard")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 13, 14, 13)
        layout.setSpacing(13)
        layout.addWidget(initials_badge(project), alignment=Qt.AlignmentFlag.AlignTop)
        text = QVBoxLayout()
        text.setSpacing(2)
        heading = QHBoxLayout()
        heading.setSpacing(9)
        heading.addWidget(label(project.name, "rowTitle"))
        heading.addWidget(label(runtime, "rowDetail"), 1, Qt.AlignmentFlag.AlignBottom)
        self.status = label("", "status")
        heading.addWidget(self.status)
        text.addLayout(heading)
        text.addWidget(label(project.description, "muted", wrap=True))
        actions = QHBoxLayout()
        actions.setSpacing(4)
        self.install = button("Install", primary=True)
        self.requirements = button("Requirements", quiet=True)
        self.repository = button("Repository ↗", quiet=True)
        actions.addWidget(self.install)
        actions.addWidget(self.requirements)
        actions.addWidget(self.repository)
        actions.addStretch()
        text.addSpacing(6)
        text.addLayout(actions)
        layout.addLayout(text, 1)


class ProjectRow(QPushButton):
    def __init__(self, project: Project):
        super().__init__()
        self.project = project
        self.drag_start: QPoint | None = None
        self.setProperty("projectRow", True)
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(72)
        self.setAccessibleName(project.name)
        self.setToolTip("Drag to reorder")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 9, 12, 9)
        layout.setSpacing(11)

        layout.addWidget(initials_badge(project))
        titles = QVBoxLayout()
        titles.setSpacing(1)
        titles.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        titles.addWidget(label(project.name, "rowTitle"))
        self.runtime = label("", "rowDetail")
        titles.addWidget(self.runtime)
        layout.addLayout(titles, 1)
        self.status = label("", "status")
        self.status.setWordWrap(True)
        self.status.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(self.status)
        layout.addWidget(label("⠿", "dragGrip"), alignment=Qt.AlignmentFlag.AlignVCenter)
        for child in self.findChildren(QLabel):
            child.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.drag_start = event.position().toPoint()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        start = self.drag_start
        if start is not None and event.buttons() & Qt.MouseButton.LeftButton and (
            event.position().toPoint() - start
        ).manhattanLength() >= QApplication.startDragDistance():
            self.drag_start = None
            self.setDown(False)
            drag = QDrag(self)
            mime = QMimeData()
            mime.setData("application/x-sunset-project", self.project.key.encode())
            drag.setMimeData(mime)
            drag.setPixmap(self.grab())
            drag.setHotSpot(start)
            drag.exec(Qt.DropAction.MoveAction)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self.drag_start = None
        super().mouseReleaseEvent(event)

    def update_status(self, status: str, runtime: str, update_detail: str = ""):
        self.runtime.setText(runtime)
        self.status.setText(status)
        self.setToolTip(f"{update_detail}\nDrag to reorder" if update_detail else "Drag to reorder")
        color = {
            "Ready": "#9ce1ce", "Failed": "#ffafa8", "Building": "#ffb895",
            "Updating": "#ffb895", "Running": "#f7a7ce", "Queued": "#dfbae9",
            "Needs setup": "#ffcf9a", "Up to date": "#9ce1ce", "Update available": "#ffcf9a", "Warm-up failed": "#ffafa8", "Warming up": "#ffb895",
            "Checking": "#dfbae9", "Upstream diverged": "#ffafa8", "Check unavailable": "#ffafa8",
            "Upstream unavailable": "#ffcf9a",
        }.get(status, "#bbb0c6")
        self.status.setStyleSheet(f"color: {color};")
        self.setAccessibleDescription(f"{runtime}, {status}")


class ProjectList(QWidget):
    reordered = Signal()

    def __init__(self):
        super().__init__()
        self.setAcceptDrops(True)
        self.entries = QVBoxLayout(self)
        self.entries.setContentsMargins(0, 3, 0, 3)
        self.entries.setSpacing(8)
        self.marker = QFrame(self)
        self.marker.setObjectName("dropMarker")
        self.marker.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.marker.hide()

    def _all_rows(self) -> list[ProjectRow]:
        return [self.entries.itemAt(index).widget() for index in range(self.entries.count())]

    def _rows(self) -> list[ProjectRow]:
        """Rows of installed layers. The others stay in place, hidden, to keep their saved order."""
        return [row for row in self._all_rows() if not row.isHidden()]

    def projects(self) -> tuple[Project, ...]:
        return tuple(row.project for row in self._all_rows())

    def _accept_move(self, event) -> bool:
        source = event.source()
        if (not event.mimeData().hasFormat("application/x-sunset-project")
                or not isinstance(source, ProjectRow) or self.entries.indexOf(source) < 0):
            return False
        event.setDropAction(Qt.DropAction.MoveAction)
        event.accept()
        return True

    def _slot_at(self, y: float) -> int:
        rows = self._rows()
        return next((index for index, row in enumerate(rows) if y < row.geometry().center().y()), len(rows))

    def dragEnterEvent(self, event):
        self._accept_move(event)

    def dragMoveEvent(self, event):
        if self._accept_move(event):
            rows = self._rows()
            slot = self._slot_at(event.position().y())
            y = rows[slot].y() - 3 if slot < len(rows) else rows[-1].geometry().bottom() + 1
            self.marker.setGeometry(0, y, self.width(), 3)
            self.marker.raise_()
            self.marker.show()

    def dragLeaveEvent(self, event):
        self.marker.hide()

    def dropEvent(self, event):
        self.marker.hide()
        if self._accept_move(event):
            self.move_row(event.source(), self._slot_at(event.position().y()))

    def move_row(self, row: ProjectRow, slot: int):
        """Move a row to a position among the visible rows."""
        visible = self._rows()
        old = self.entries.indexOf(row)
        target = self.entries.indexOf(visible[slot]) if slot < len(visible) else self.entries.count()
        destination = target - (target > old)
        if destination != old:
            self.entries.removeWidget(row)
            self.entries.insertWidget(destination, row)
            self.reordered.emit()


class Job(QObject):
    line = Signal(str)
    state = Signal(str, str)
    update_check = Signal(str, str, str)
    shader_usage = Signal(str, object)
    # None marks the start of a warm-up, then each parsed progress line follows.
    progress = Signal(str, object)
    warmed = Signal(str, str, str)
    complete = Signal(str)

    def __init__(
        self, library: Library, projects: tuple[Project, ...], action: str,
        game: str, proton: ProtonInstall | None, prepared_prefix: Path | None, launch_options: str = "",
        *, console_mode: str | None = None, warm_jobs: int | None = None, warm_first: bool = False,
    ):
        super().__init__()
        self.library = library
        self.projects = projects
        self.action = action
        self.game = game
        self.proton = proton
        self.prepared_prefix = prepared_prefix
        self.launch_options = launch_options
        self.console_mode = console_mode
        self.warm_jobs = warm_jobs
        self.warm_first = warm_first
        self.runner = Runner(self.line.emit)

    def _warm(self, project: Project, output) -> None:
        def watch(message: str):
            output(message)
            for line in message.splitlines():
                if (progress := parse_warmup_progress(line)) is not None:
                    self.progress.emit(project.key, progress)

        self.state.emit(project.key, "Warming up")
        self.progress.emit(project.key, None)
        output("Warming up shaders…\n")
        self.runner.output = watch
        try:
            revision = self.library.warm_up(
                project, self.game, self.proton, self.runner, prepared_prefix=self.prepared_prefix,
                options=self.launch_options, console_mode=self.console_mode, jobs=self.warm_jobs,
            )
        finally:
            self.runner.output = output
        self.warmed.emit(project.key, revision, self.game)
        output("Shader warm-up finished.\n")

    @Slot()
    def execute(self):
        failures = 0
        setup_needed = 0
        try:
            logs = self.library.data / "logs"
            read_only = self.action in {"check", "shader_scan"}
            shader_job = self.action in {"shader_scan", "shader_clear"}
            with (nullcontext((None, None)) if read_only else open_run_log(logs)) as (path, log):
                def output(message: str):
                    if log is not None:
                        log.write(message)
                    self.line.emit(message)

                if path is not None:
                    try:
                        prune_run_logs(logs, current=path)
                    except OSError as error:
                        output(f"Could not remove older launcher logs: {error}\n")

                self.runner.output = output
                for project in self.projects:
                    self.runner.check_cancelled()
                    verb = {
                        "build": "Building", "update": "Updating", "launch": "Running", "warm": "Warming up",
                        "delete": "Deleting", "check": "Checking",
                        "shader_scan": "Measuring shader cache", "shader_clear": "Clearing shader cache",
                    }[self.action]
                    if not shader_job:
                        self.state.emit(project.key, verb)
                    output(f"\n[{project.name}] {verb}\n")
                    try:
                        if self.action == "check":
                            check = self.library.check_update(project, self.runner)
                            output(f"{check.status}. Upstream commit {check.revision}.\n")
                        elif self.action == "warm":
                            self._warm(project, output)
                        elif self.action == "launch":
                            if self.warm_first:
                                try:
                                    self._warm(project, output)
                                except (OSError, OperationError, ValueError) as error:
                                    output(f"Shader warm-up did not finish: {error}\nStarting the game.\n")
                                self.state.emit(project.key, "Running")
                            self.library.launch(
                                project, self.game, self.proton, self.runner,
                                prepared_prefix=self.prepared_prefix, options=self.launch_options,
                                console_mode=self.console_mode,
                            )
                            output("Exited successfully.\n")
                        elif self.action == "delete":
                            self.library.delete_build(project, self.runner)
                        elif self.action == "shader_scan":
                            self.shader_usage.emit(project.key, self.library.shader_usage(project, self.runner))
                        elif self.action == "shader_clear":
                            self.shader_usage.emit(project.key, self.library.clear_shader_cache(project, self.runner))
                        else:
                            revision = self.library.build(project, self.action == "update", self.runner)
                            output(f"Built revision {revision}.\n")
                    except Cancelled:
                        if not shader_job and self.action != "check":
                            self.state.emit(project.key, "Stopped")
                        raise
                    except SetupRequired as error:
                        setup_needed += 1
                        if self.action == "check":
                            self.update_check.emit(project.key, "Check unavailable", str(error))
                            self.state.emit(project.key, "Check unavailable")
                        elif not shader_job:
                            self.state.emit(project.key, "Needs setup")
                        output(f"Setup required: {error}\n")
                    except (OSError, OperationError, ValueError) as error:
                        failures += 1
                        if self.action == "check":
                            self.update_check.emit(project.key, "Check unavailable", str(error))
                            self.state.emit(project.key, "Check unavailable")
                        elif self.action == "warm":
                            # Only for the job: the row then shows its update status again, since the build is fine.
                            self.state.emit(project.key, "Warm-up failed")
                        elif not shader_job:
                            self.state.emit(project.key, "Failed")
                        output(f"Error: {error}\n")
                    else:
                        if self.action == "check":
                            self.update_check.emit(project.key, check.status, f"Upstream commit {check.revision}")
                            self.state.emit(project.key, check.status)
                        elif not shader_job:
                            self.state.emit(project.key, "Not built" if self.action == "delete" else "Ready")
                    finally:
                        if self.action == "launch" and (recipe := recipe_for(project)) and recipe.shader_cache:
                            try:
                                # A stopped game may have written cache files before it exited.
                                size = self.library.shader_usage(project, Runner(output))
                                self.shader_usage.emit(project.key, size)
                            except OSError as error:
                                output(f"Could not measure the shader cache: {error}\n")
                if self.action == "check" and (setup_needed or failures):
                    result = f"Update check complete · {setup_needed + failures} unavailable"
                elif setup_needed or failures:
                    result = " · ".join(
                        message for count, message in (
                            (setup_needed, f"Setup needed for {setup_needed}"),
                            (failures, f"{failures} failed"),
                        ) if count
                    )
                else:
                    result = {
                        "launch": "Launch finished", "delete": "Build removed", "check": "Update check complete",
                        "warm": "Shader warm-up finished",
                        "shader_scan": "Shader cache measured", "shader_clear": "Shader cache cleanup complete",
                    }.get(self.action, "Build finished")
        except Cancelled as error:
            self.line.emit(f"\n{error}\n")
            result = "Stopped"
        except Exception as error:
            self.line.emit(f"\nError: {error}\n")
            result = "Job failed"
        self.complete.emit(result)


class MainWindow(QMainWindow):
    def __init__(
        self, data: Path, *, scan_runtime: bool = True, check_updates_on_startup: bool = True,
        check_launcher_release: bool = False,
    ):
        super().__init__()
        self.library = Library(data)
        self.settings = QSettings(str(data / "preferences.ini"), QSettings.Format.IniFormat)
        self.selected = next(
            (project for project in available_projects() if project.key == self.settings.value("project")),
            available_projects()[0],
        )
        self.thread: QThread | None = None
        self.worker: Job | None = None
        self._initial_check_pending = check_updates_on_startup
        # Measure shader caches once the startup check, or a job that preempted it, finishes.
        self._startup_scan_pending = check_updates_on_startup
        self._startup_check_running = False
        self._job_after_startup_check: Job | None = None
        self._close_after_startup_check = False
        self.states: dict[str, str] = {}
        self.shader_sizes: dict[str, int] = {}
        self.update_checks: dict[str, tuple[str, str]] = {}
        self.rows: dict[str, ProjectRow] = {}
        self.install_cards: dict[str, InstallCard] = {}
        self.warm_tracker = WarmupTracker()
        self.warm_key: str | None = None
        self.warm_outcome = ""
        self._last_result = ""
        self._installing: set[str] = set()
        self.setWindowTitle("Sunset Launcher")
        self.setWindowIcon(QIcon(sun_logo()))
        self.setMinimumSize(870, 620)
        self.resize(1130, 790)

        content = QWidget()
        self.setCentralWidget(content)
        layout = QHBoxLayout(content)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.pages = QStackedWidget()
        layout.addWidget(self._sidebar())
        layout.addWidget(self.pages, 1)
        self.pages.addWidget(self._library_page())
        self.pages.addWidget(self._install_page())
        self.pages.addWidget(self._warmup_page())
        self.pages.addWidget(self._activity_page())
        self.memory_timer = QTimer(self)
        self.memory_timer.setInterval(1000)
        self.memory_timer.timeout.connect(self._update_memory)
        self.memory_timer.start()
        try:
            prune_run_logs(self.library.data / "logs")
        except OSError as error:
            self._write_log(f"Could not remove older launcher logs: {error}\n")
        if scan_runtime:
            self.scan_proton()
        self._select(self.selected)
        if check_launcher_release:
            self._check_launcher_release()

    def _check_launcher_release(self):
        """Look for a newer launcher release without blocking the window or the job queue."""
        found: list[str | None] = []
        worker = threading.Thread(target=lambda: found.append(fetch_latest_release()), daemon=True)
        poll = QTimer(self)
        poll.setInterval(500)

        def collect():
            if worker.is_alive():
                return
            poll.stop()
            poll.deleteLater()
            if found and found[0]:
                self._show_launcher_release(found[0])

        poll.timeout.connect(collect)
        worker.start()
        poll.start()

    def _show_launcher_release(self, tag: str):
        self.launcher_update.setText(f"{tag} available ↗")
        self.launcher_update.setToolTip(f"A newer launcher is out. You have v{__version__}. Open the {tag} release page.")
        self.launcher_update.show()

    def _sidebar(self) -> QWidget:
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(210)
        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(15, 25, 15, 14)
        layout.setSpacing(7)
        branding = QHBoxLayout()
        branding.setSpacing(9)
        mark = QLabel()
        mark.setPixmap(sun_logo().scaled(32, 32, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        branding.addWidget(mark)
        branding.addWidget(label("Sunset Launcher", "brand"), 1)
        layout.addLayout(branding)
        version = label(f"v{__version__}", "muted")
        version.setIndent(45)
        layout.addWidget(version)
        layout.addSpacing(12)

        self.nav = QButtonGroup(self)
        for index, title in enumerate(("Library", "Install", "Warm-up", "Activity")):
            tab = NavButton(title)
            self.nav.addButton(tab, index)
            layout.addWidget(tab)
        self.nav.button(LIBRARY_PAGE).setChecked(True)
        self.nav.idClicked.connect(self._show_page)

        layout.addStretch()
        self.launcher_update = button("", quiet=True)
        self.launcher_update.clicked.connect(lambda: open_url(QUrl(f"{RELEASES_URL}/releases/latest")))
        self.launcher_update.hide()
        layout.addWidget(self.launcher_update)
        self.summary = label("Idle", "muted", wrap=True)
        self.summary.setIndent(4)
        # Keep room for a three-line message so a long one never loses its last line.
        self.summary.setMinimumHeight(self.summary.fontMetrics().lineSpacing() * 3 + 4)
        self.summary.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom)
        self.summary.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        layout.addWidget(self.summary)
        layout.addWidget(rule())
        data = button("Open data folder", quiet=True)
        data.clicked.connect(lambda: self._open_folder(self.library.data))
        layout.addWidget(data)
        return sidebar

    def _show_page(self, index: int):
        self.nav.button(index).setChecked(True)
        self.pages.setCurrentIndex(index)
        if index == WARMUP_PAGE:
            self._update_memory()

    def _library_page(self) -> QWidget:
        page = QWidget()
        page.setObjectName("libraryPage")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(24, 23, 24, 23)
        layout.setSpacing(13)
        layout.addWidget(Hero())
        layout.addLayout(self._shared_game_controls())
        header = QHBoxLayout()
        header.addWidget(label("Projects", "sectionTitle"), 1)
        self.check_updates = button("Check for updates")
        self.check_updates.setToolTip("Compare each built project's commit with its upstream branch.")
        self.check_updates.clicked.connect(self._check_for_updates)
        header.addWidget(self.check_updates)
        self.update_all = button("Update && build all")
        self.update_all.setToolTip("Update and build every installed layer with an available upstream.")
        self.update_all.clicked.connect(lambda: self._start(self._updatable_projects(), "update"))
        header.addWidget(self.update_all)
        layout.addLayout(header)

        self.empty_library = QFrame()
        self.empty_library.setObjectName("card")
        empty = QVBoxLayout(self.empty_library)
        empty.setContentsMargins(22, 20, 22, 20)
        empty.setSpacing(6)
        empty.addWidget(label("No layers installed yet", "rowTitle"))
        empty.addWidget(label(
            "Open Install to pick a compatibility layer. Once it builds, it appears here with its "
            "launch, update and shader cache controls.", "muted", wrap=True,
        ))
        browse_layers = button("Browse layers", primary=True)
        browse_layers.clicked.connect(lambda: self._show_page(INSTALL_PAGE))
        empty.addSpacing(6)
        empty.addWidget(browse_layers, alignment=Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(self.empty_library)

        self.library_grid = QWidget()
        grid = QHBoxLayout(self.library_grid)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(15)
        self.project_list = ProjectList()
        self.selection = QButtonGroup(self)
        pending = {project.key: project for project in available_projects()}
        restored = []
        for key in self.settings.value("projectOrder", [], type=list):
            if isinstance(key, str) and key in pending:
                restored.append(pending.pop(key))
        for project in (*restored, *pending.values()):
            row = ProjectRow(project)
            row.clicked.connect(lambda checked=False, item=project: self._select(item))
            self.selection.addButton(row)
            self.rows[project.key] = row
            self.project_list.entries.addWidget(row)
        self.project_list.reordered.connect(self._save_project_order)
        grid.addWidget(self.project_list, 5, Qt.AlignmentFlag.AlignTop)
        detail = self._details()
        detail.setMinimumWidth(365)
        # A stretch below keeps the panel at its natural height beside a taller list. Top alignment alone
        # would size it from its size hint and clip wrapped text.
        column = QVBoxLayout()
        column.setSpacing(0)
        column.addWidget(detail)
        column.addStretch()
        grid.addLayout(column, 6)
        layout.addWidget(self.library_grid)
        layout.addStretch()

        scroll = QScrollArea()
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidgetResizable(True)
        scroll.setWidget(page)
        return scroll

    def _details(self) -> QFrame:
        detail = QFrame()
        detail.setObjectName("detail")
        layout = QVBoxLayout(detail)
        layout.setContentsMargins(18, 15, 18, 17)
        layout.setSpacing(8)

        heading = QHBoxLayout()
        titles = QVBoxLayout()
        titles.setSpacing(0)
        self.project_name = label("", "projectTitle")
        self.revision = label("", "status")
        titles.addWidget(self.project_name)
        titles.addWidget(self.revision)
        heading.addLayout(titles, 1)
        # Launch sits beside the name so it stays in view however long the panel gets.
        self.launch = button("Launch", primary=True)
        self.launch.setMinimumWidth(110)
        self.launch.clicked.connect(lambda: self._start((self.selected,), "launch"))
        heading.addWidget(self.launch, alignment=Qt.AlignmentFlag.AlignVCenter)
        layout.addLayout(heading)
        self.description = label("", "muted", wrap=True)
        layout.addWidget(self.description)
        sources = QHBoxLayout()
        sources.setSpacing(2)
        self.repository = button("Repository ↗", quiet=True)
        self.repository.clicked.connect(lambda: open_url(QUrl(self.selected.repository)))
        setup = button("Build setup", quiet=True)
        setup.clicked.connect(lambda: self._show_setup(self.selected))
        self.checkout = button("Build folder", quiet=True)
        self.checkout.clicked.connect(lambda: self._open_folder(self.library.source(self.selected)))
        for link in (self.repository, setup, self.checkout):
            link.setProperty("link", True)
            sources.addWidget(link)
        sources.addStretch()
        layout.addLayout(sources)

        layout.addWidget(section("Build"))
        builds = QHBoxLayout()
        builds.setSpacing(8)
        self.update = button("Update && build")
        self.update.setToolTip("Fetch upstream changes, fast-forward the checkout, then compile.")
        self.update.clicked.connect(lambda: self._start((self.selected,), "update"))
        self.build = button("Rebuild")
        self.build.setToolTip("Compile the local checkout again.")
        self.build.clicked.connect(lambda: self._start((self.selected,), "build"))
        self.delete = button("Delete build…", quiet=True)
        self.delete.setProperty("danger", True)
        self.delete.setToolTip("Remove the recorded build outputs. The layer moves back to Install.")
        self.delete.clicked.connect(self._confirm_delete)
        builds.addWidget(self.update)
        builds.addWidget(self.build)
        builds.addStretch()
        builds.addWidget(self.delete)
        layout.addLayout(builds)

        layout.addWidget(section("Shaders"))
        self.shader_cache_size = label("Shader cache", "muted", wrap=True)
        layout.addWidget(self.shader_cache_size)
        cache = QHBoxLayout()
        cache.setSpacing(8)
        self.open_warmup = button("Warm up…")
        self.open_warmup.setToolTip("Compile this layer's shaders for the shared game before playing.")
        self.open_warmup.clicked.connect(lambda: self._open_warmup(self.selected))
        self.measure_shaders = button("Refresh size")
        self.measure_shaders.setToolTip("Measure generated shaders and pipeline caches for this project again.")
        self.measure_shaders.clicked.connect(lambda: self._start((self.selected,), "shader_scan"))
        self.clear_shaders = button("Clear cache…", quiet=True)
        self.clear_shaders.setProperty("danger", True)
        self.clear_shaders.setToolTip("Remove generated shaders and pipeline caches for this project.")
        self.clear_shaders.clicked.connect(self._confirm_shader_clear)
        cache.addWidget(self.open_warmup)
        cache.addWidget(self.measure_shaders)
        cache.addStretch()
        cache.addWidget(self.clear_shaders)
        layout.addLayout(cache)

        layout.addWidget(section("Play"))
        self.runtime_box = QWidget()
        runtime = QVBoxLayout(self.runtime_box)
        runtime.setContentsMargins(0, 0, 0, 0)
        runtime.setSpacing(4)
        title = label("Proton version")
        title.setProperty("fieldLabel", True)
        runtime.addWidget(title)
        self.proton = QComboBox()
        self.proton.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.proton.setMinimumWidth(100)
        title.setBuddy(self.proton)
        self.proton.currentIndexChanged.connect(self._save_proton)
        runtime.addWidget(self.proton)
        links = QHBoxLayout()
        links.setSpacing(2)
        self.rescan = button("Rescan Steam", quiet=True)
        self.rescan.clicked.connect(self.scan_proton)
        self.choose_proton = button("Add Proton…", quiet=True)
        self.choose_proton.clicked.connect(self._browse_proton)
        self.prefix = button("Open prefix", quiet=True)
        self.prefix.clicked.connect(self._prefix_action)
        for link in (self.rescan, self.choose_proton, self.prefix):
            link.setProperty("link", True)
            links.addWidget(link)
        links.addStretch()
        runtime.addLayout(links)
        layout.addWidget(self.runtime_box)

        self.console_mode_box = QWidget()
        mode_layout = QHBoxLayout(self.console_mode_box)
        mode_layout.setContentsMargins(0, 2, 0, 2)
        mode_title = label("Console")
        mode_title.setProperty("fieldLabel", True)
        self.console_mode = SegmentedControl()
        self.console_mode.setToolTip("The console model the game sees, which sets its memory and device type.")
        mode_title.setBuddy(self.console_mode)
        self.console_mode.activated.connect(self._save_console_mode)
        mode_layout.addWidget(mode_title)
        mode_layout.addStretch()
        mode_layout.addWidget(self.console_mode)
        layout.addWidget(self.console_mode_box)

        options_title = label("Launch options")
        options_title.setProperty("fieldLabel", True)
        layout.addWidget(options_title)
        self.launch_options = QLineEdit()
        self.launch_options.setClearButtonEnabled(True)
        options_title.setBuddy(self.launch_options)
        self.launch_options.textChanged.connect(self._save_launch_options)
        layout.addWidget(self.launch_options)

        self.note = label("", "note", wrap=True)
        layout.addWidget(self.note)
        return detail

    @staticmethod
    def _scroll(page: QWidget) -> QScrollArea:
        scroll = QScrollArea()
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidgetResizable(True)
        scroll.setWidget(page)
        return scroll

    def _install_page(self) -> QWidget:
        page = QWidget()
        page.setObjectName("installPage")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(24, 23, 24, 23)
        layout.setSpacing(12)
        layout.addWidget(label("Install a layer", "sectionTitle"))
        layout.addWidget(label(
            "Each layer is cloned from its upstream repository and built with your local tools. "
            "Open Requirements first to see what it needs. A layer moves to your library once it builds.",
            "muted", wrap=True,
        ))
        layout.addSpacing(4)
        for project in available_projects():
            card = InstallCard(project, self._runtime_name(project))
            card.install.clicked.connect(lambda checked=False, item=project: self._install(item))
            card.requirements.clicked.connect(lambda checked=False, item=project: self._show_setup(item))
            card.repository.clicked.connect(lambda checked=False, item=project: open_url(QUrl(item.repository)))
            self.install_cards[project.key] = card
            layout.addWidget(card)
        self.all_installed = label("Every available layer is installed. Delete a build to move it back here.", "muted", wrap=True)
        layout.addWidget(self.all_installed)
        layout.addStretch()
        return self._scroll(page)

    def _warmup_page(self) -> QWidget:
        page = QWidget()
        page.setObjectName("warmupPage")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(24, 23, 24, 23)
        layout.setSpacing(12)
        layout.addWidget(label("Shader warm-up", "sectionTitle"))
        layout.addWidget(label(
            "Compile a game's shaders before you play, so it stutters less while they build. Each layer runs its "
            "own warm-up with the same Proton prefix, shader cache and settings as Launch.",
            "muted", wrap=True,
        ))
        layout.addSpacing(4)

        target = QFrame()
        target.setObjectName("card")
        form = QGridLayout(target)
        form.setContentsMargins(15, 13, 15, 13)
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(9)
        layer_title = label("Layer")
        layer_title.setProperty("fieldLabel", True)
        self.warm_layer = QComboBox()
        layer_title.setBuddy(self.warm_layer)
        self.warm_layer.activated.connect(self._warm_layer_chosen)
        form.addWidget(layer_title, 0, 0)
        form.addWidget(self.warm_layer, 0, 1, 1, 2)
        game_title = label("Game")
        game_title.setProperty("fieldLabel", True)
        self.warm_game = label("", "muted")
        self.warm_game.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.warm_change_game = button("Change…", quiet=True)
        self.warm_change_game.clicked.connect(lambda: self._browse_game(self.warm_change_game))
        form.addWidget(game_title, 1, 0)
        form.addWidget(self.warm_game, 1, 1)
        form.addWidget(self.warm_change_game, 1, 2)
        self.warm_note = label("", "note", wrap=True)
        form.addWidget(self.warm_note, 2, 0, 1, 3)
        form.setColumnStretch(1, 1)
        layout.addWidget(target)

        card = QFrame()
        card.setObjectName("warmCard")
        progress = QVBoxLayout(card)
        progress.setContentsMargins(16, 14, 16, 15)
        progress.setSpacing(9)
        top = QHBoxLayout()
        self.warm_title = label("Ready to warm up", "rowTitle")
        self.warm_subject = label("", "muted")
        self.warm_percent = label("", "warmPercent")
        self.warm_button = button("Warm up", primary=True)
        self.warm_button.setMinimumWidth(92)
        self.warm_button.clicked.connect(self._warm_button_clicked)
        top.addWidget(self.warm_title)
        top.addWidget(self.warm_subject, 1, Qt.AlignmentFlag.AlignBottom)
        top.addWidget(self.warm_percent)
        top.addSpacing(8)
        top.addWidget(self.warm_button)
        progress.addLayout(top)
        self.warm_bar = QProgressBar()
        self.warm_bar.setTextVisible(False)
        self.warm_bar.setFixedHeight(6)
        self.warm_bar.setRange(0, 1000)
        self.warm_bar.setValue(0)
        progress.addWidget(self.warm_bar)
        self.warm_stage = label("", "status")
        progress.addWidget(self.warm_stage)
        stats = QHBoxLayout()
        stats.setSpacing(10)
        self.warm_stats: dict[str, QLabel] = {}
        self.warm_captions: dict[str, QLabel] = {}
        for key, caption in (("done", "Done"), ("speed", "Speed"), ("left", "Time left"),
                             ("free", "Free RAM"), ("used", "Warm-up RAM")):
            column = QVBoxLayout()
            column.setSpacing(1)
            self.warm_captions[key] = label(caption, "statCaption")
            self.warm_stats[key] = label("—", "statValue")
            column.addWidget(self.warm_captions[key])
            column.addWidget(self.warm_stats[key])
            stats.addLayout(column, 1)
        progress.addLayout(stats)
        layout.addWidget(card)

        settings = QFrame()
        settings.setObjectName("card")
        options = QGridLayout(settings)
        options.setContentsMargins(15, 13, 15, 13)
        options.setHorizontalSpacing(14)
        options.setVerticalSpacing(7)
        jobs_title = label("Parallel compiles")
        jobs_title.setProperty("fieldLabel", True)
        options.addWidget(jobs_title, 0, 0)
        options.addWidget(label(
            "Each compile can hold over a gigabyte of RAM in the driver. Lower this if free RAM runs low.",
            "muted", wrap=True,
        ), 1, 0)
        self.warm_jobs = QComboBox()
        jobs_title.setBuddy(self.warm_jobs)
        for jobs in WARMUP_JOBS:
            self.warm_jobs.addItem("Automatic" if jobs == 0 else str(jobs), jobs)
        saved_jobs = self.settings.value("warmup/jobs", 0, type=int)
        self.warm_jobs.setCurrentIndex(max(0, self.warm_jobs.findData(saved_jobs)))
        self.warm_jobs.activated.connect(lambda: self.settings.setValue("warmup/jobs", self.warm_jobs.currentData()))
        options.addWidget(self.warm_jobs, 0, 1, 2, 1, Qt.AlignmentFlag.AlignVCenter)
        options.addWidget(rule(), 2, 0, 1, 2)
        self.warm_before_launch = QCheckBox("Warm up before launch after an update")
        self.warm_before_launch.setChecked(self.settings.value("warmup/beforeLaunch", True, type=bool))
        self.warm_before_launch.toggled.connect(lambda on: self.settings.setValue("warmup/beforeLaunch", on))
        options.addWidget(self.warm_before_launch, 3, 0, 1, 2)
        options.addWidget(label(
            "Launch first runs the warm-up when the layer was rebuilt or the shared game changed since the last one, "
            "and skips it when nothing changed.", "muted", wrap=True,
        ), 4, 0, 1, 2)
        options.setColumnStretch(0, 1)
        layout.addWidget(settings)
        layout.addStretch()
        return self._scroll(page)

    def _activity_page(self) -> QWidget:
        page = QWidget()
        page.setObjectName("activityPage")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(24, 23, 24, 23)
        layout.setSpacing(12)
        header = QHBoxLayout()
        header.addWidget(label("Activity", "sectionTitle"), 1)
        self.stop = button("Stop job")
        self.stop.clicked.connect(self._stop)
        self.stop.setEnabled(False)
        header.addWidget(self.stop)
        logs = button("Open saved logs")
        logs.setToolTip("Open the current and previous launcher job logs.")
        logs.clicked.connect(lambda: self._open_folder(self.library.data / "logs", create=True))
        header.addWidget(logs)
        layout.addLayout(header)
        self.console = QPlainTextEdit()
        self.console.setObjectName("log")
        self.console.setReadOnly(True)
        self.console.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.console.setMaximumBlockCount(5000)
        self.console.setPlaceholderText("Build and launch output will appear here.")
        self.console.setAccessibleName("Build and launch output")
        layout.addWidget(self.console, 1)
        return page

    @staticmethod
    def _runtime_name(project: Project) -> str:
        recipe = recipe_for(project)
        if recipe is None:
            return "Unavailable on this OS"
        return {"proton": "Proton", "managed": f"Managed by {project.name}", "native": "Native"}[recipe.runtime]

    def _select(self, project: Project):
        self.selected = project
        self.settings.setValue("project", project.key)
        self.rows[project.key].setChecked(True)
        self.project_name.setText(project.name)
        self.description.setText(project.description)
        recipe = recipe_for(project)
        uses_proton = recipe is not None and recipe.runtime == "proton"
        self.runtime_box.setVisible(uses_proton)
        note = "\n\n".join(filter(None, (project.upstream_unavailable, recipe.launch_note if recipe else "")))
        self.note.setText(note)
        self.note.setVisible(bool(note))
        self.repository.setEnabled(not project.upstream_unavailable)
        self.repository.setToolTip(project.upstream_unavailable)
        self._restore_proton()
        modes = recipe.console_modes if recipe is not None else ()
        self.console_mode_box.setVisible(bool(modes))
        self.console_mode.clear()
        for mode in modes:
            self.console_mode.addItem(CONSOLE_LABELS.get(mode, mode.replace("-", " ").title()), mode)
        if modes:
            saved_mode = self.settings.value(f"consoleMode/{project.key}", modes[0], type=str)
            self.console_mode.setCurrentIndex(max(0, self.console_mode.findData(saved_mode)))
        self.launch_options.setText(self.settings.value(f"launchOptions/{project.key}", "", type=str))
        self.launch_options.setPlaceholderText(recipe.options_hint if recipe is not None else "")
        self.launch_options.setToolTip(
            "Saved for this project. Leading VAR=value entries set environment variables. "
            f"Quote values that contain spaces.\n{recipe.options_hint if recipe is not None else ''}".rstrip()
        )
        self.launch_options.setEnabled(recipe is not None)
        self._refresh()

    def _save_console_mode(self):
        self.settings.setValue(f"consoleMode/{self.selected.key}", self.console_mode.currentData())

    def _save_launch_options(self):
        self.settings.setValue(f"launchOptions/{self.selected.key}", self.launch_options.text().strip())

    def _installed(self, project: Project) -> bool:
        return recipe_for(project) is not None and self.library.revision(project) is not None

    def _installed_projects(self) -> tuple[Project, ...]:
        return tuple(project for project in self.project_list.projects() if self._installed(project))

    def _refresh(self):
        installed = self._installed_projects()
        for project in self.project_list.projects():
            self.rows[project.key].setVisible(project in installed)
        if installed and self.selected not in installed:
            # A deleted build leaves the library. Show the next installed layer instead.
            self._select(installed[0])
            return
        self.empty_library.setVisible(not installed)
        self.library_grid.setVisible(bool(installed))
        self._refresh_install_cards(installed)
        self._refresh_warmup_layers(installed)
        self.nav.button(LIBRARY_PAGE).set_badge(str(len(installed)) if installed else "")
        waiting = sum(not card.isHidden() for card in self.install_cards.values())
        self.nav.button(INSTALL_PAGE).set_badge(str(waiting) if waiting else "")
        for project in self.project_list.projects():
            revision = self.library.revision(project)
            recipe = recipe_for(project)
            update_status, detail = self.update_checks.get(project.key, ("Ready", "")) if revision else ("Not built", "")
            if project.upstream_unavailable:
                update_status, detail = "Upstream unavailable", project.upstream_unavailable
            status = self.states.get(project.key) or ("Unsupported" if recipe is None else update_status)
            update_detail = f"Built commit {revision}\n{detail}" if revision and detail else detail
            self.rows[project.key].update_status(status, self._runtime_name(project), update_detail)
        revision = self.library.revision(self.selected)
        self.revision.setText(f"Commit {revision[:7]}" if revision else "")
        _, detail = self.update_checks.get(self.selected.key, ("", ""))
        self.revision.setToolTip(f"Built commit {revision}\n{detail}".rstrip() if revision else "")
        self.checkout.setEnabled(self.library.source(self.selected).is_dir())
        recipe = recipe_for(self.selected)
        supports_prepared = recipe is not None and bool(recipe.prepared_wine_env)
        prepared = self._prepared_prefix()
        self.prefix.setEnabled(supports_prepared or self.library.prefix(self.selected).is_dir())
        self.prefix.setText(
            "Prepared prefix ✓" if prepared is not None else "Wine prefix…" if supports_prepared else "Open prefix"
        )
        self.prefix.setToolTip(str(prepared) if prepared is not None else "")
        self._refresh_actions()

    def _refresh_install_cards(self, installed: tuple[Project, ...]):
        for key, card in self.install_cards.items():
            project = card.project
            card.setVisible(project not in installed)
            state = self.states.get(key, "")
            has_checkout = (self.library.source(project) / ".git").exists()
            if project.upstream_unavailable and not has_checkout:
                status = "Upstream unavailable"
            elif state:
                status = state
            else:
                status = "Downloaded, not built" if has_checkout else ""
            card.status.setText(status)
            card.install.setText("Retry" if state in {"Failed", "Needs setup", "Stopped"} or has_checkout else "Install")
            card.install.setToolTip(project.upstream_unavailable or "Clone the upstream source and build it.")
            card.repository.setEnabled(not project.upstream_unavailable)
        self.all_installed.setVisible(all(card.isHidden() for card in self.install_cards.values()))

    def _refresh_warmup_layers(self, installed: tuple[Project, ...]):
        layers = [project for project in installed if (recipe := recipe_for(project)) and recipe.warmup]
        current = self.warm_layer.currentData()
        self.warm_layer.blockSignals(True)
        self.warm_layer.clear()
        for project in layers:
            self.warm_layer.addItem(project.name, project)
        if not layers:
            self.warm_layer.addItem("No installed layer has a warm-up")
        preferred = self.warm_key and next((p for p in layers if p.key == self.warm_key), None)
        chosen = preferred or (current if current in layers else self.selected if self.selected in layers else None)
        if chosen is not None:
            self.warm_layer.setCurrentIndex(layers.index(chosen))
        self.warm_layer.blockSignals(False)
        self._update_warm_card()

    def _warm_layer_chosen(self):
        project = self.warm_layer.currentData()
        if project is not None and project != self.selected:
            # Warm-up uses the layer's own Proton, prefix and launch settings, which follow the selection.
            self._select(project)
        self._update_warm_card()

    def _open_warmup(self, project: Project):
        self._show_page(WARMUP_PAGE)
        index = self.warm_layer.findText(project.name)
        if index >= 0:
            self.warm_layer.setCurrentIndex(index)
        self._update_warm_card()

    def _install(self, project: Project):
        self._installing.add(project.key)
        self._start((project,), "build")

    def _warm_key(self, project: Project) -> str:
        return f"warmup/{project.key}"

    def _last_warmup(self, project: Project) -> tuple[str, str, str]:
        key = self._warm_key(project)
        return tuple(self.settings.value(f"{key}/{name}", "", type=str) for name in ("revision", "game", "time"))

    def _needs_warmup(self, project: Project, game: str) -> bool:
        recipe = recipe_for(project)
        if recipe is None or not recipe.warmup:
            return False
        revision, warmed_game, _ = self._last_warmup(project)
        return revision != self.library.revision(project) or warmed_game != game

    def _warm_button_clicked(self):
        if self.warm_key is not None:
            self._stop()
            return
        project = self.warm_layer.currentData()
        if project is not None:
            if project != self.selected:
                self._select(project)
            self._start((project,), "warm")

    def _update_memory(self):
        if self.pages.currentIndex() != WARMUP_PAGE and self.warm_key is None:
            return
        if self.warm_key is not None:
            # Elapsed time and the estimate move on between Victory's ten-second progress lines.
            self._update_warm_card()
        self.warm_stats["free"].setText(format_bytes(available_memory()))
        pid = self.worker.runner.pid if self.warm_key is not None and self.worker is not None else None
        used = process_tree_memory(pid) if pid else None
        self.warm_stats["used"].setText(format_bytes(used) if used is not None else "—")

    def _update_warm_card(self):
        if not hasattr(self, "warm_button"):
            return
        project = next((item for item in PROJECTS if item.key == self.warm_key), None) or self.warm_layer.currentData()
        recipe = recipe_for(project) if project is not None else None
        game = self.game.text().strip()
        self.warm_game.setText(game or "Choose a shared game path in Library or with Change…")
        self.warm_game.setToolTip(game)
        active = self.warm_key is not None
        tracker = self.warm_tracker
        progress = tracker.progress if active or self.warm_outcome else None
        kind = recipe.warmup_progress if recipe is not None else "none"
        self.warm_note.setText({
            "total": "Shows overall progress and time left.",
            "file": "Works through the game's files one at a time. Progress and time left are for the current file.",
            "count": "Reports how many shaders are cached so far, without a total, so there is no time left estimate.",
            "none": "Prepares shaders with its own per-title profiles and reports no counts. Follow it in Activity.",
        }[kind] if recipe is not None and recipe.warmup else "Install XCL, Victory, AvatarManz, Cool Fox or TuxBox to warm up shaders.")
        self.warm_captions["left"].setText("Time left (file)" if kind == "file" else "Time left")
        self.warm_subject.setText(project.name if project is not None else "")
        if active:
            self.warm_title.setText("Warming up shaders")
        elif self.warm_outcome:
            self.warm_title.setText(self.warm_outcome)
        else:
            self.warm_title.setText("Ready to warm up")
        fraction = tracker.fraction if progress is not None else None
        if active and fraction is None:
            self.warm_bar.setRange(0, 0)
            self.warm_percent.setText("")
        else:
            self.warm_bar.setRange(0, 1000)
            if self.warm_outcome == "Warm-up finished" and fraction is None:
                fraction = 1.0
            self.warm_bar.setValue(int((fraction or 0) * 1000))
            self.warm_percent.setText(f"{fraction:.0%}" if fraction is not None else "")
        if progress is not None:
            unit = progress.unit
            done = f"{progress.done:,} / {progress.total:,}" if progress.total is not None else f"{progress.done:,}"
            self.warm_stats["done"].setText(done)
            speed = tracker.speed
            self.warm_stats["speed"].setText(f"{speed:,.0f} {unit}/s" if speed is not None else "—")
            self.warm_stats["left"].setText(format_duration(tracker.seconds_left) if active else "—")
        else:
            for key in ("done", "speed", "left"):
                self.warm_stats[key].setText("—")
        if active:
            stage = (progress.stage, progress.unit) if progress is not None else ("Starting",)
            self.warm_stage.setText(" · ".join(filter(None, (*stage, f"{format_duration(tracker.elapsed)} elapsed"))))
        elif project is not None:
            revision, warmed_game, when = self._last_warmup(project)
            if revision and warmed_game == game and revision == self.library.revision(project):
                self.warm_stage.setText(f"Warmed up for this game on {when}. Run it again to pick up new shaders.")
            elif revision and warmed_game == game:
                self.warm_stage.setText("The layer changed since its last warm-up for this game.")
            else:
                self.warm_stage.setText("Not warmed up for this game yet.")
        else:
            self.warm_stage.setText("")
        self.warm_button.setText("Cancel" if active else "Warm up")
        self.warm_button.setObjectName("" if active else "primary")
        self.warm_button.style().unpolish(self.warm_button)
        self.warm_button.style().polish(self.warm_button)
        badge = (f"{fraction:.0%}" if fraction is not None else "…") if active else ""
        self.nav.button(WARMUP_PAGE).set_badge(badge)

    @Slot()
    def _refresh_actions(self):
        if not hasattr(self, "launch"):
            return
        can_start_job = self.thread is None or (
            self._startup_check_running and self._job_after_startup_check is None and not self._close_after_startup_check
        )
        recipe = recipe_for(self.selected)
        proton = self._chosen_proton()
        has_game = bool(self.game.text().strip())
        ready = self.library.revision(self.selected) is not None
        upstream_unavailable = self.selected.upstream_unavailable
        has_checkout = (self.library.source(self.selected) / ".git").exists()
        self.update_all.setEnabled(can_start_job and bool(self._updatable_projects()))
        self.check_updates.setEnabled(self.thread is None and bool(self._built_projects()))
        self.build.setEnabled(can_start_job and recipe is not None and (not upstream_unavailable or has_checkout))
        self.build.setToolTip(
            "Compile the existing local checkout." if upstream_unavailable and has_checkout
            else upstream_unavailable or "Clone on first build, then compile the local checkout."
        )
        self.update.setEnabled(can_start_job and recipe is not None and not upstream_unavailable and has_checkout)
        self.update.setToolTip(upstream_unavailable or "Fetch upstream changes, fast-forward the checkout, then compile.")
        self.delete.setEnabled(can_start_job and recipe is not None and self.library.has_build_record(self.selected))
        cache_supported = recipe is not None and recipe.shader_cache is not None
        cache_size = self.shader_sizes.get(self.selected.key)
        self.shader_cache_size.setText(
            "Shader cache" if cache_size is None else f"Shader cache · {QLocale().formattedDataSize(cache_size, 1)}"
        )
        self.measure_shaders.setEnabled(can_start_job and cache_supported)
        self.open_warmup.setVisible(recipe is not None and bool(recipe.warmup))
        for card in self.install_cards.values():
            unavailable = card.project.upstream_unavailable and not (self.library.source(card.project) / ".git").exists()
            card.install.setEnabled(can_start_job and recipe_for(card.project) is not None and not unavailable)
        if hasattr(self, "warm_button"):
            layer = self.warm_layer.currentData()
            layer_recipe = recipe_for(layer) if layer is not None else None
            warm_ready = (
                can_start_job and layer is not None and has_game
                and (layer_recipe.runtime != "proton" or self._chosen_proton() is not None)
            )
            self.warm_button.setEnabled(self.warm_key is not None or bool(warm_ready))
            self.warm_button.setToolTip(
                "Stop the warm-up." if self.warm_key is not None
                else "Choose a shared game path first." if not has_game
                else "Select a Proton version for this layer in Library." if layer_recipe and layer_recipe.runtime == "proton" and self._chosen_proton() is None
                else "Compile this layer's shaders for the shared game."
            )
        self.clear_shaders.setEnabled(can_start_job and cache_supported and bool(cache_size))
        self.proton.setEnabled(self.proton.count() > 1)
        can_launch = can_start_job and recipe is not None and ready and has_game and (recipe.runtime != "proton" or proton is not None)
        self.launch.setEnabled(can_launch)
        if recipe is None:
            reason = "No build recipe is available for this operating system."
        elif not ready:
            reason = "Build the project before launching."
        elif not has_game:
            reason = "Choose a shared game path."
        elif recipe.runtime == "proton" and proton is None:
            reason = "Select an installed Proton version."
        else:
            reason = "Launch the shared game." if self.selected.game_input == "either" else "Launch the shared game executable."
        self.launch.setToolTip(reason)

    def _shared_game_controls(self) -> QHBoxLayout:
        controls = QHBoxLayout()
        title = label("Shared game path")
        title.setProperty("fieldLabel", True)
        controls.addWidget(title)
        self.game = QLineEdit(self._saved_game_path())
        title.setBuddy(self.game)
        self.game.setPlaceholderText("Choose a game executable for all projects…")
        self.game.setToolTip("All projects share this path. XCL and TuxBox can also use a game folder.")
        self.game.textChanged.connect(self._refresh_actions)
        self.game.textChanged.connect(self._update_warm_card)
        self.game.editingFinished.connect(self._save_game)
        controls.addWidget(self.game, 1)
        self.browse = button("Browse…")
        self.browse.clicked.connect(lambda: self._browse_game(self.browse))
        controls.addWidget(self.browse)
        return controls

    def _saved_game_path(self) -> str:
        if self.settings.contains("gamePath"):
            return self.settings.value("gamePath", "", type=str)

        legacy_order = dict.fromkeys((self.selected.key, *(project.key for project in PROJECTS)))
        paths = [self.settings.value(f"game/{key}", "", type=str).strip() for key in legacy_order]
        paths = [path for path in paths if path]
        # An executable can be reused by every project. Keep the selected project's path first among matches.
        path = next((path for path in paths if isfile(expanduser(path))), None)
        if path is None:
            path = next((path for path in paths if isdir(expanduser(path))), paths[0] if paths else "")
        self.settings.setValue("gamePath", path)
        return path

    def _save_game(self):
        self.settings.setValue("gamePath", self.game.text().strip())

    def _save_project_order(self):
        self.settings.setValue("projectOrder", [project.key for project in self.project_list.projects()])
        self.settings.sync()

    def _updatable_projects(self) -> tuple[Project, ...]:
        return tuple(project for project in self._installed_projects() if not project.upstream_unavailable)

    def _built_projects(self) -> tuple[Project, ...]:
        return tuple(project for project in self._updatable_projects() if self.library.revision(project))

    def _check_for_updates(self):
        self._start(self._built_projects(), "check")

    def _cached_projects(self) -> tuple[Project, ...]:
        return tuple(project for project in self._installed_projects() if recipe_for(project).shader_cache is not None)

    @Slot()
    def _check_for_updates_on_startup(self):
        if not self.isVisible():
            return
        if built := self._built_projects():
            self._start(built, "check", startup=True)
        else:
            self._scan_shaders_on_startup()

    def _scan_shaders_on_startup(self):
        self._startup_scan_pending = False
        self._start(self._cached_projects(), "shader_scan", startup=True)

    def _confirm_delete(self):
        project = self.selected
        if not self.delete.isEnabled():
            return
        box = QMessageBox(QMessageBox.Icon.Warning, "Delete build", f"Delete the {project.name} build?", parent=self)
        box.setInformativeText("This removes recorded build files. Your game files, source checkout and saves are kept.")
        delete = box.addButton("Delete build", QMessageBox.ButtonRole.DestructiveRole)
        cancel = box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(cancel)
        box.setEscapeButton(cancel)
        box.exec()
        if box.clickedButton() is delete:
            self._start((project,), "delete")

    def _confirm_shader_clear(self):
        if not self.clear_shaders.isEnabled():
            return
        project = self.selected
        box = QMessageBox(QMessageBox.Icon.Warning, "Clear shader cache", f"Clear the {project.name} shader cache?", parent=self)
        message = "The project will regenerate shaders and pipelines as needed on future runs."
        if recipe_for(project).shader_cache == "tuxbox":
            message += " This includes TuxBox’s default shader cache shared with standalone TuxBox launches."
        box.setInformativeText(message)
        clear = box.addButton("Clear cache", QMessageBox.ButtonRole.DestructiveRole)
        cancel = box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(cancel)
        box.setEscapeButton(cancel)
        box.exec()
        if box.clickedButton() is clear:
            self._start((project,), "shader_clear")

    def _browse_game(self, anchor: QWidget | None = None):
        anchor = anchor or self.browse
        menu = QMenu(self)
        menu.addAction("Choose a game executable…", lambda: self._pick_game(False))
        menu.addAction("Choose a game folder…", lambda: self._pick_game(True))
        menu.exec(anchor.mapToGlobal(anchor.rect().bottomLeft()))

    def _pick_game(self, directory: bool):
        existing = self.game.text().strip()
        if directory:
            picked = QFileDialog.getExistingDirectory(self, "Choose a game folder", existing)
        else:
            picked, _ = QFileDialog.getOpenFileName(
                self, "Choose a game executable", existing, "Windows executables (*.exe *.EXE);;All files (*)",
            )
        if picked:
            self.game.setText(picked)
            self._save_game()

    def _chosen_proton(self) -> ProtonInstall | None:
        return self.proton.currentData()

    def _restore_proton(self):
        chosen = self.settings.value(f"proton/{self.selected.key}", "", type=str)
        self.proton.blockSignals(True)
        self.proton.setCurrentIndex(next(
            (idx for idx in range(1, self.proton.count()) if str(self.proton.itemData(idx).executable) == chosen),
            0,
        ))
        self.proton.blockSignals(False)

    def _save_proton(self):
        proton = self._chosen_proton()
        self.settings.setValue(f"proton/{self.selected.key}", str(proton.executable) if proton else "")
        self._refresh_actions()

    def scan_proton(self):
        if not proton_supported():
            return
        by_path = {item.executable: item for item in discover_proton()}
        for path in self.settings.value("proton/customPaths", [], type=list):
            if Path(path) not in by_path:
                try:
                    custom = custom_proton(path)
                except (OSError, ValueError):
                    continue
                by_path[custom.executable] = custom

        self.proton.blockSignals(True)
        self.proton.clear()
        self.proton.addItem("Select an installed version…" if by_path else "No Proton versions found")
        for install in by_path.values():
            self.proton.addItem(f"{install.name} · {install.source}", install)
            self.proton.setItemData(self.proton.count() - 1, str(install.executable), Qt.ItemDataRole.ToolTipRole)
        self.proton.blockSignals(False)
        self._restore_proton()
        self._refresh_actions()

    def _browse_proton(self):
        chosen, _ = QFileDialog.getOpenFileName(self, "Choose the executable proton script")
        if not chosen:
            return
        try:
            install = custom_proton(chosen)
        except (OSError, ValueError) as error:
            QMessageBox.warning(self, "Invalid Proton path", str(error))
            return
        paths = self.settings.value("proton/customPaths", [], type=list)
        path = str(install.executable)
        if path not in paths:
            self.settings.setValue("proton/customPaths", [*paths, path])
        self.settings.setValue(f"proton/{self.selected.key}", path)
        self.scan_proton()

    def _prepared_prefix(self) -> Path | None:
        path = self.settings.value(f"winePrefix/{self.selected.key}", "", type=str)
        return Path(path) if path else None

    def _prefix_action(self):
        recipe = recipe_for(self.selected)
        prefix = self._prepared_prefix()
        active = prefix or self.library.prefix(self.selected)
        if recipe is None or not recipe.prepared_wine_env:
            self._open_folder(active)
            return
        menu = QMenu(self)
        menu.addAction("Choose prepared Wine prefix…", self._choose_prepared_prefix)
        menu.addAction("Open active prefix", lambda: self._open_folder(active)).setEnabled(active.is_dir())
        menu.addAction("Use standard Proton", lambda: self._save_prepared_prefix("")).setEnabled(prefix is not None)
        menu.exec(self.prefix.mapToGlobal(self.prefix.rect().bottomLeft()))

    def _choose_prepared_prefix(self):
        default = self._prepared_prefix() or Path.home()
        chosen = QFileDialog.getExistingDirectory(self, "Choose a prepared Wine prefix", str(default))
        if chosen:
            try:
                prefix = prepared_wine_prefix(Path(chosen))
            except (OSError, ValueError) as error:
                QMessageBox.warning(self, "Invalid Wine prefix", str(error))
                return
            self._save_prepared_prefix(str(prefix))

    def _save_prepared_prefix(self, path: str):
        self.settings.setValue(f"winePrefix/{self.selected.key}", path)
        self._refresh()

    def _show_setup(self, project: Project):
        recipe = recipe_for(project)
        box = QMessageBox(self)
        box.setWindowTitle(f"{project.name} setup")
        box.setText(f"{project.name} build setup")
        box.setTextFormat(Qt.TextFormat.PlainText)
        instructions = ["Install Git and add the build tools to PATH."]
        if recipe is None:
            instructions = ["This operating system has no build recipe for this project."]
        else:
            instructions.extend(recipe.requirements)
        box.setInformativeText("\n\n".join(instructions))
        guide = None
        title_guide = None
        if not project.upstream_unavailable:
            guide = box.addButton("Open upstream guide", QMessageBox.ButtonRole.ActionRole)
            if recipe is not None and recipe.launch_url:
                title_guide = box.addButton("Open launch guide", QMessageBox.ButtonRole.ActionRole)
        box.addButton(QMessageBox.StandardButton.Close)
        box.exec()
        if guide is not None and box.clickedButton() is guide:
            open_url(QUrl(project.setup_url))
        elif recipe is not None and title_guide is not None and box.clickedButton() is title_guide:
            open_url(QUrl(recipe.launch_url))

    def _open_folder(self, path: Path, *, create: bool = False):
        try:
            if create:
                path.mkdir(parents=True, exist_ok=True)
            if not path.is_dir() or not open_url(QUrl.fromLocalFile(str(path))):
                raise OSError(f"Could not open {path}")
        except OSError as error:
            QMessageBox.warning(self, "Folder unavailable", str(error))

    def _start(self, projects: tuple[Project, ...], action: str, *, startup: bool = False):
        if not projects:
            return
        if self.thread is not None and (
            not self._startup_check_running or action == "check" or self._job_after_startup_check is not None
            or self._close_after_startup_check
        ):
            return
        self._save_game()
        self._save_launch_options()
        game = self.game.text().strip()
        options = self.launch_options.text().strip()
        if action in {"launch", "warm"}:
            try:
                self.library.game_path(projects[0], game)
            except (OperationError, OSError, ValueError) as error:
                QMessageBox.warning(self, "Game files unavailable", str(error))
                return
            try:
                parse_launch_options(options)
            except OperationError as error:
                QMessageBox.warning(self, "Launch options", str(error))
                return
        uses_layer = action in {"launch", "warm"}
        jobs = self.warm_jobs.currentData() or None
        job = Job(
            self.library, projects, action, game, self._chosen_proton(), self._prepared_prefix(), options,
            console_mode=self.console_mode.currentData() if uses_layer else None,
            warm_jobs=jobs if uses_layer else None,
            warm_first=action == "launch" and self.warm_before_launch.isChecked() and self._needs_warmup(projects[0], game),
        )
        if self.thread is not None:
            self._job_after_startup_check = job
            self._stop()
            self._refresh_actions()
            return
        self._begin_job(job, startup=startup)

    def _begin_job(self, job: Job, *, startup: bool = False):
        action = job.action
        shader_job = action in {"shader_scan", "shader_clear"}
        for project in job.projects:
            if action in {"build", "update", "delete"}:
                self.update_checks.pop(project.key, None)
            if shader_job or action == "launch":
                self.shader_sizes.pop(project.key, None)
            if not shader_job and not startup:
                self.states[project.key] = "Queued"
        self._set_summary(
            "Job queued" if not startup else "Measuring shader caches…" if shader_job else "Checking for updates…"
        )
        self.stop.setEnabled(True)

        self._startup_check_running = startup
        self.thread = QThread(self)
        self.worker = job
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.execute)
        self.worker.line.connect(self._write_log)
        self.worker.state.connect(self._job_state)
        self.worker.update_check.connect(self._job_update_check)
        self.worker.shader_usage.connect(self._job_shader_usage)
        self.worker.progress.connect(self._job_progress)
        self.worker.warmed.connect(self._job_warmed)
        self.worker.complete.connect(self._job_complete)
        self.worker.complete.connect(self.thread.quit)
        self.thread.finished.connect(self.worker.deleteLater)
        self.thread.finished.connect(self._job_finished)
        self.thread.finished.connect(self.thread.deleteLater)
        self.thread.start()
        self._refresh()

    @Slot(str)
    def _write_log(self, message: str):
        bar = self.console.verticalScrollBar()
        follow = bar.value() == bar.maximum()
        cursor = QTextCursor(self.console.document())
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText(message)
        if follow:
            bar.setValue(bar.maximum())

    @Slot(str, object)
    def _job_progress(self, key: str, progress: WarmupProgress | None):
        if progress is None:
            self.warm_key = key
            self.warm_outcome = ""
            self.warm_tracker.reset()
            self._refresh_warmup_layers(self._installed_projects())
        else:
            self.warm_tracker.update(progress)
        self._update_warm_card()
        self._update_memory()

    @Slot(str, str, str)
    def _job_warmed(self, key: str, revision: str, game: str):
        prefix = f"warmup/{key}"
        self.settings.setValue(f"{prefix}/revision", revision)
        self.settings.setValue(f"{prefix}/game", game)
        self.settings.setValue(f"{prefix}/time", QLocale().toString(QDateTime.currentDateTime(), QLocale.FormatType.ShortFormat))
        self.warm_outcome = "Warm-up finished"
        self.warm_key = None
        self._update_warm_card()

    @Slot(str, str)
    def _job_state(self, key: str, state: str):
        self.states[key] = state
        if state in {"Building", "Updating", "Running", "Deleting", "Checking", "Warming up"}:
            name = next(project.name for project in PROJECTS if project.key == key)
            self._set_summary(f"{state} {name}")
        self._refresh()

    @Slot(str, str, str)
    def _job_update_check(self, key: str, status: str, detail: str):
        self.update_checks[key] = (status, detail)

    @Slot(str)
    def _job_complete(self, result: str):
        self._last_result = result
        self._set_summary(result)
        self._write_log(f"\n{result}.\n")

    @Slot(str, object)
    def _job_shader_usage(self, key: str, size: int):
        self.shader_sizes[key] = size
        self._refresh_actions()

    @Slot()
    def _job_finished(self):
        # finished is emitted just before the thread exits. Let it exit before a deferred close quits the app.
        if self.thread is not None:
            self.thread.wait()
        self.thread = None
        self.worker = None
        self._startup_check_running = False
        self.states = {key: state for key, state in self.states.items() if state in {"Failed", "Needs setup", "Stopped"}}
        self.stop.setEnabled(False)
        if self.warm_key is not None:
            # The warm-up ended without finishing: stopped, failed, or skipped by its launch.
            self.warm_outcome = "Warm-up stopped" if self._last_result == "Stopped" else "Warm-up did not finish"
            self.warm_key = None
        newly_installed = [key for key in self._installing if self.library.revision(self._project(key)) is not None]
        self._installing.clear()
        if newly_installed:
            self._set_summary(f"{self._project(newly_installed[-1]).name} installed")
            self._select(self._project(newly_installed[-1]))
        if self._close_after_startup_check:
            self._close_after_startup_check = False
            self.close()
            return
        job, self._job_after_startup_check = self._job_after_startup_check, None
        if job is not None:
            self._begin_job(job)
            return
        if self._startup_scan_pending and self.isVisible():
            self._scan_shaders_on_startup()
            if self.thread is not None:
                return
        self._refresh()

    def _set_summary(self, text: str):
        self.summary.setText(text)
        self.summary.setToolTip(text)

    @staticmethod
    def _project(key: str) -> Project:
        return next(project for project in PROJECTS if project.key == key)

    def _stop(self):
        if self.worker is not None:
            self.stop.setEnabled(False)
            self._set_summary("Stopping…")
            self.worker.runner.cancel()

    def showEvent(self, event: QShowEvent):
        super().showEvent(event)
        if self._initial_check_pending:
            self._initial_check_pending = False
            QTimer.singleShot(0, self, self._check_for_updates_on_startup)

    def closeEvent(self, event: QCloseEvent):
        if self.thread is not None:
            event.ignore()
            if self._startup_check_running:
                self._close_after_startup_check = True
                self._job_after_startup_check = None
                self._stop()
                self._refresh_actions()
                return
            answer = QMessageBox.question(
                self, "Job still running", "Stop the current job? You can close the launcher once it stops.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer == QMessageBox.StandardButton.Yes:
                self._stop()
            return
        self._save_game()
        self._save_launch_options()
        self.settings.sync()
        super().closeEvent(event)
