"""Live terminal progress for each kind of test."""

from __future__ import annotations

import os
import sys
import threading
import time
from collections import Counter
from typing import Self, TextIO


def _enable_windows_vt() -> bool:
    if os.name != "nt":
        return True
    try:
        import ctypes

        kernel = ctypes.windll.kernel32
        handle = kernel.GetStdHandle(-11)
        mode = ctypes.c_uint()
        if not kernel.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(kernel.SetConsoleMode(handle, mode.value | 0x0004))
    except (AttributeError, OSError):
        return False


class ProgressDisplay:
    def __init__(self, stream: TextIO | None = None, interval: float = 0.5) -> None:
        self.stream = stream or sys.stdout
        self.interval = interval
        self.interactive = self.stream.isatty()
        self.vt = self.interactive and _enable_windows_vt()
        self.stage = "Escaneando projeto"
        self.totals: Counter[str] = Counter()
        self.done: Counter[str] = Counter()
        self.started = time.monotonic()
        self._lock = threading.Lock()
        self._paint_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lines = 0
        self._line_width = 0
        self._last_signature: tuple | None = None

    def __enter__(self) -> Self:
        return self.start()

    def start(self) -> ProgressDisplay:
        if self.interactive:
            self._thread = threading.Thread(target=self._animate, daemon=True)
            self._thread.start()
        else:
            self.stream.write(f"{self.stage}...\n")
            self.stream.flush()
        return self

    def __exit__(self, *_: object) -> None:
        self.finish()

    def finish(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        if self.interactive:
            self._render(final=True)
            if not self.vt:
                self.stream.write("\n")
                self.stream.flush()

    def set_stage(self, stage: str, totals: dict[str, int] | None = None) -> None:
        if self.interactive and self.totals:
            self._render()
        with self._lock:
            self.stage = stage
            self.started = time.monotonic()
            self.totals = Counter(totals or {})
            self.done = Counter()
            self._last_signature = None
        if not self.interactive:
            self.stream.write(f"{stage}...\n")
            self.stream.flush()

    def advance(self, label: str) -> None:
        with self._lock:
            self.done[label] += 1

    def _animate(self) -> None:
        while not self._stop.wait(self.interval):
            self._render()

    def _render(self, final: bool = False) -> None:
        with self._paint_lock:
            with self._lock:
                stage = self.stage
                totals = self.totals.copy()
                done = self.done.copy()
                elapsed = int(time.monotonic() - self.started)
            signature = (stage, tuple(totals.items()), tuple(done.items()), elapsed, final)
            if signature == self._last_signature:
                return
            self._last_signature = signature
            fraction = sum(done.values()) / sum(totals.values()) if totals else 0.0
            if final:
                fraction = 1.0
            spinner = "|/-\\"[elapsed % 4]
            title = "Concluído" if final else stage
            header = f"Identity {spinner} {title}  {elapsed // 60:02d}:{elapsed % 60:02d}"
            header_color = self._color(fraction if totals or final else min(elapsed / 6, 0.66))
            lines = [self._paint(header, header_color)]
            if totals:
                lines.append(self._colored_bar("Total", sum(done.values()), sum(totals.values())))
                for label, total in totals.items():
                    lines.append(self._colored_bar(label, done[label], total))
            if self.vt:
                if self._lines:
                    self.stream.write(f"\x1b[{self._lines}F")
                for index in range(max(self._lines, len(lines))):
                    self.stream.write("\x1b[2K" + (lines[index] if index < len(lines) else "") + "\n")
                self._lines = max(self._lines, len(lines))
            else:
                line = header + ("  " + self._bar("Total", sum(done.values()), sum(totals.values())) if totals else "")
                self.stream.write("\r" + line.ljust(self._line_width))
                self._line_width = len(line)
            self.stream.flush()
            if final:
                self._lines = 0

    def _colored_bar(self, label: str, done: int, total: int) -> str:
        fraction = min(done / total, 1) if total else 1.0
        return self._paint(self._bar(label, done, total), self._color(fraction))

    def _paint(self, text: str, color: str) -> str:
        return f"\x1b[{color}m{text}\x1b[0m" if self.vt else text

    @staticmethod
    def _color(fraction: float) -> str:
        if fraction < 1 / 3:
            return "31"
        if fraction < 2 / 3:
            return "33"
        return "32"

    @staticmethod
    def _bar(label: str, done: int, total: int) -> str:
        fraction = min(done / total, 1) if total else 1
        filled = round(fraction * 22)
        return f"  {label[:26]:26} [{'#' * filled}{'-' * (22 - filled)}] {done}/{total} {fraction:5.1%}"
