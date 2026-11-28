"""Speed and time-left estimates for shader warm-up progress."""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable

from .backend import WarmupProgress

# Estimates follow the last half minute, so they settle quickly when compiles speed up or slow down.
WINDOW = 30.0


class WarmupTracker:
    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self.clock = clock
        self.reset()

    def reset(self) -> None:
        self.started = self.clock()
        self.paused_at: float | None = None
        self.progress: WarmupProgress | None = None
        self._samples: deque[tuple[float, int]] = deque()

    def pause(self) -> None:
        if self.paused_at is None:
            self.paused_at = self.clock()

    def resume(self) -> None:
        """Leave the pause out of the elapsed time, and measure speed again from here."""
        if self.paused_at is not None:
            self.started += self.clock() - self.paused_at
            self.paused_at = None
        self._samples.clear()

    def update(self, progress: WarmupProgress) -> None:
        previous = self.progress
        # Victory restarts its count for each game file. A new file or total starts a new estimate.
        if previous is None or (progress.stage, progress.total) != (previous.stage, previous.total) or progress.done < previous.done:
            self._samples.clear()
        now = self.clock()
        self._samples.append((now, progress.done))
        while len(self._samples) > 2 and now - self._samples[0][0] > WINDOW:
            self._samples.popleft()
        self.progress = progress

    @property
    def fraction(self) -> float | None:
        progress = self.progress
        if progress is None or not progress.total:
            return None
        return min(progress.done / progress.total, 1.0)

    @property
    def speed(self) -> float | None:
        """Items per second over the recent window."""
        if len(self._samples) < 2:
            return None
        (start, first), (end, last) = self._samples[0], self._samples[-1]
        if end <= start or last < first:
            return None
        return (last - first) / (end - start)

    @property
    def seconds_left(self) -> float | None:
        progress, speed = self.progress, self.speed
        if progress is None or progress.total is None or not speed:
            return None
        return max(progress.total - progress.done, 0) / speed

    @property
    def elapsed(self) -> float:
        return (self.paused_at if self.paused_at is not None else self.clock()) - self.started

    @property
    def paused(self) -> bool:
        return self.paused_at is not None


def format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    seconds = int(round(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, seconds = divmod(rest, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {seconds:02d}s"
    return f"{seconds}s"


def format_bytes(size: int | None) -> str:
    if size is None:
        return "—"
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}" if unit in {"GB"} or value < 10 and unit != "B" else f"{value:.0f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"
