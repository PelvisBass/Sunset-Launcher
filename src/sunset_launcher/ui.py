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

from PySide6.QtCore import QLocale, QMimeData, QPoint, QRectF, QSettings, QThread, QTimer, Qt, QUrl, QObject, Signal, Slot
from PySide6.QtGui import QColor, QCloseEvent, QDesktopServices, QDrag, QIcon, QLinearGradient, QPainter, QPainterPath, QPixmap, QShowEvent, QTextCursor
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QComboBox, QFileDialog, QFrame, QHBoxLayout, QLabel,
    QLineEdit, QMainWindow, QMenu, QMessageBox, QPlainTextEdit, QPushButton, QScrollArea,
    QSizePolicy, QStackedWidget, QVBoxLayout, QWidget,
)

from . import RELEASES_URL, __version__
from .backend import (
    Cancelled, Library, OperationError, Runner, SetupRequired, host_environment, parse_launch_options, recipe_for,
)
from .catalog import PROJECTS, Project
from .logs import open_run_log, prune_run_logs
from .proton import ProtonInstall, custom_proton, discover_proton, prepared_wine_prefix, proton_supported


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

        initials = label(project.name[0].upper())
        initials.setAlignment(Qt.AlignmentFlag.AlignCenter)
        initials.setFixedSize(39, 39)
        accent = QColor(project.accent)
        tint = QColor(53, 36, 61)
        initials.setStyleSheet(
            f"background: {tint.name()}; color: {accent.name()}; border-radius: 9px;"
            "font-size: 19px; font-weight: 800;"
        )
        layout.addWidget(initials)
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
            "Needs setup": "#ffcf9a", "Up to date": "#9ce1ce", "Update available": "#ffcf9a",
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

    def _rows(self) -> list[ProjectRow]:
        return [self.entries.itemAt(index).widget() for index in range(self.entries.count())]

    def projects(self) -> tuple[Project, ...]:
        return tuple(row.project for row in self._rows())

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
        old = self.entries.indexOf(row)
        destination = slot - (slot > old)
        if destination != old:
            self.entries.removeWidget(row)
            self.entries.insertWidget(destination, row)
            self.reordered.emit()


class Job(QObject):
    line = Signal(str)
    state = Signal(str, str)
    update_check = Signal(str, str, str)
    shader_usage = Signal(str, object)
    complete = Signal(str)

    def __init__(
        self, library: Library, projects: tuple[Project, ...], action: str,
        game: str, proton: ProtonInstall | None, prepared_prefix: Path | None, launch_options: str = "",
        *, console_mode: str | None = None,
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
        self.runner = Runner(self.line.emit)

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
                        "build": "Building", "update": "Updating", "launch": "Running",
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
                        elif self.action == "launch":
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
        self.pages.addWidget(self._activity_page())
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
        for index, title in enumerate(("Library", "Activity")):
            tab = button(title)
            tab.setCheckable(True)
            tab.setProperty("nav", True)
            self.nav.addButton(tab, index)
            layout.addWidget(tab)
        self.nav.button(0).setChecked(True)
        self.nav.idClicked.connect(self.pages.setCurrentIndex)

        layout.addStretch()
        self.launcher_update = button("", quiet=True)
        self.launcher_update.clicked.connect(lambda: open_url(QUrl(f"{RELEASES_URL}/releases/latest")))
        self.launcher_update.hide()
        layout.addWidget(self.launcher_update)
        self.summary = label("Idle", "muted", wrap=True)
        self.summary.setIndent(4)
        layout.addWidget(self.summary)
        layout.addWidget(rule())
        data = button("Open data folder", quiet=True)
        data.clicked.connect(lambda: self._open_folder(self.library.data))
        layout.addWidget(data)
        return sidebar

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
        self.update_all.setToolTip("Clone missing projects and update projects with an available upstream.")
        self.update_all.clicked.connect(lambda: self._start(self._updatable_projects(), "update"))
        header.addWidget(self.update_all)
        layout.addLayout(header)

        grid = QHBoxLayout()
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
        # Keep the panel at its natural height when the project list is taller.
        grid.addWidget(detail, 6, Qt.AlignmentFlag.AlignTop)
        layout.addLayout(grid)
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
        layout.setContentsMargins(15, 13, 15, 13)
        layout.setSpacing(7)

        heading = QHBoxLayout()
        self.project_name = label("", "projectTitle")
        heading.addWidget(self.project_name, 1)
        self.revision = label("", "status")
        heading.addWidget(self.revision)
        layout.addLayout(heading)
        self.description = label("", "muted", wrap=True)
        layout.addWidget(self.description)
        sources = QHBoxLayout()
        sources.setSpacing(0)
        self.repository = button("Repository ↗", quiet=True)
        self.repository.clicked.connect(lambda: open_url(QUrl(self.selected.repository)))
        self.checkout = button("Build folder", quiet=True)
        self.checkout.clicked.connect(lambda: self._open_folder(self.library.source(self.selected)))
        setup = button("Build setup", quiet=True)
        setup.clicked.connect(self._show_setup)
        sources.addWidget(self.repository)
        sources.addWidget(setup)
        sources.addWidget(self.checkout)
        sources.addStretch()
        layout.addLayout(sources)

        builds = QHBoxLayout()
        self.build = button("Build", primary=True)
        self.build.setToolTip("Clone on first build, then compile the local checkout.")
        self.build.clicked.connect(lambda: self._start((self.selected,), "build"))
        self.update = button("Update && build")
        self.update.setToolTip("Fetch upstream changes, fast-forward the checkout, then compile.")
        self.update.clicked.connect(lambda: self._start((self.selected,), "update"))
        self.delete = button("Delete build…", quiet=True)
        self.delete.setToolTip("Remove the recorded build outputs for this project.")
        self.delete.clicked.connect(self._confirm_delete)
        builds.addWidget(self.build)
        builds.addWidget(self.update)
        builds.addWidget(self.delete)
        builds.addStretch()
        layout.addLayout(builds)

        cache = QHBoxLayout()
        self.shader_cache_size = label("Shader cache", "muted", wrap=True)
        self.measure_shaders = button("Refresh size", quiet=True)
        self.measure_shaders.setToolTip("Measure generated shaders and pipeline caches for this project again.")
        self.measure_shaders.clicked.connect(lambda: self._start((self.selected,), "shader_scan"))
        self.clear_shaders = button("Clear cache…", quiet=True)
        self.clear_shaders.setToolTip("Remove generated shaders and pipeline caches for this project.")
        self.clear_shaders.clicked.connect(self._confirm_shader_clear)
        cache.addWidget(self.shader_cache_size, 1)
        cache.addWidget(self.measure_shaders)
        cache.addWidget(self.clear_shaders)
        layout.addLayout(cache)
        layout.addWidget(rule())

        self.runtime_box = QWidget()
        runtime = QVBoxLayout(self.runtime_box)
        runtime.setContentsMargins(0, 0, 0, 0)
        runtime.setSpacing(3)
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
        links.setSpacing(0)
        self.rescan = button("Rescan Steam", quiet=True)
        self.rescan.clicked.connect(self.scan_proton)
        self.choose_proton = button("Add Proton…", quiet=True)
        self.choose_proton.clicked.connect(self._browse_proton)
        self.prefix = button("Open prefix", quiet=True)
        self.prefix.clicked.connect(self._prefix_action)
        links.addWidget(self.rescan)
        links.addWidget(self.choose_proton)
        links.addWidget(self.prefix)
        links.addStretch()
        runtime.addLayout(links)
        layout.addWidget(self.runtime_box)

        self.console_mode_box = QWidget()
        mode_layout = QHBoxLayout(self.console_mode_box)
        mode_layout.setContentsMargins(0, 0, 0, 0)
        mode_title = label("Console mode")
        mode_title.setProperty("fieldLabel", True)
        self.console_mode = QComboBox()
        mode_title.setBuddy(self.console_mode)
        self.console_mode.activated.connect(self._save_console_mode)
        mode_layout.addWidget(mode_title)
        mode_layout.addWidget(self.console_mode, 1)
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
        launch_row = QHBoxLayout()
        self.launch = button("Launch", primary=True)
        self.launch.clicked.connect(lambda: self._start((self.selected,), "launch"))
        self.launch.setMinimumWidth(87)
        launch_row.addWidget(self.launch)
        launch_row.addStretch()
        layout.addLayout(launch_row)
        return detail

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
            self.console_mode.addItem(mode.replace("-", " ").title(), mode)
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

    def _refresh(self):
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
        self.game.editingFinished.connect(self._save_game)
        controls.addWidget(self.game, 1)
        self.browse = button("Browse…")
        self.browse.clicked.connect(self._browse_game)
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
        return tuple(
            project for project in self.project_list.projects()
            if recipe_for(project) is not None and not project.upstream_unavailable
        )

    def _built_projects(self) -> tuple[Project, ...]:
        return tuple(project for project in self._updatable_projects() if self.library.revision(project))

    def _check_for_updates(self):
        self._start(self._built_projects(), "check")

    def _cached_projects(self) -> tuple[Project, ...]:
        return tuple(
            project for project in self.project_list.projects()
            if (recipe := recipe_for(project)) is not None and recipe.shader_cache is not None
        )

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

    def _browse_game(self):
        menu = QMenu(self)
        menu.addAction("Choose a game executable…", lambda: self._pick_game(False))
        menu.addAction("Choose a game folder…", lambda: self._pick_game(True))
        menu.exec(self.browse.mapToGlobal(self.browse.rect().bottomLeft()))

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

    def _show_setup(self):
        project = self.selected
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
        if action == "launch":
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
        job = Job(
            self.library, projects, action, game, self._chosen_proton(), self._prepared_prefix(), options,
            console_mode=self.console_mode.currentData() if action == "launch" else None,
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
        self.summary.setText(
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

    @Slot(str, str)
    def _job_state(self, key: str, state: str):
        self.states[key] = state
        if state in {"Building", "Updating", "Running", "Deleting", "Checking"}:
            name = next(project.name for project in PROJECTS if project.key == key)
            self.summary.setText(f"{state} {name}")
        self._refresh()

    @Slot(str, str, str)
    def _job_update_check(self, key: str, status: str, detail: str):
        self.update_checks[key] = (status, detail)

    @Slot(str)
    def _job_complete(self, result: str):
        self.summary.setText(result)
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

    def _stop(self):
        if self.worker is not None:
            self.stop.setEnabled(False)
            self.summary.setText("Stopping…")
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
