"""Live terminal progress for each kind of test."""

from __future__ import annotations

import os
import shutil
import sys
import threading
import time
from collections import Counter
from typing import Self, TextIO

from .terminal_ui import panel


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
    def __init__(self, stream: TextIO | None = None, interval: float = 0.2) -> None:
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

    def _write(self, value: str) -> None:
        encoding = getattr(self.stream, "encoding", None)
        if encoding:
            value = value.encode(encoding, errors="replace").decode(encoding)
        self.stream.write(value)

    def __enter__(self) -> Self:
        return self.start()

    def show_intro(self, delay: float = 0.018, show_tagline: bool = False) -> None:
        """Reveal the brand once on a real terminal, without polluting captured output."""
        if not self.interactive:
            return
        if not self.vt:
            self._write("IDENTITY\n")
            if show_tagline:
                self._write("SCAN  /  TEST  /  REPORT\n")
            self._write("\n")
            self.stream.flush()
            return
        for length in range(1, len("IDENTITY") + 1):
            title = " ".join("IDENTITY"[:length])
            self._write(f"\r\x1b[2K\x1b[1;92m  {title}\x1b[0m")
            self.stream.flush()
            if delay:
                time.sleep(delay)
        self._write("\n")
        if show_tagline:
            self._write("\x1b[32m  SCAN  /  TEST  /  REPORT\x1b[0m\n")
        self._write("\n")
        self.stream.flush()

    def start(self) -> ProgressDisplay:
        if self.interactive:
            self._render()
            self._thread = threading.Thread(target=self._animate, daemon=True)
            self._thread.start()
        else:
            self._write(f"{self.stage}...\n")
            self.stream.flush()
        return self

    def __exit__(self, *_: object) -> None:
        self.finish()

    def finish(self, status: str = "Finalizado") -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        if self.interactive:
            self._render(final=True, ending=status)
            if not self.vt:
                self._write("\n")
                self.stream.flush()

    def set_stage(self, stage: str, totals: dict[str, int] | None = None) -> None:
        with self._lock:
            self.stage = stage
            self.started = time.monotonic()
            self.totals = Counter(totals or {})
            self.done = Counter()
            self._last_signature = None
        if not self.interactive:
            self._write(f"{stage}...\n")
            self.stream.flush()
        elif self._thread and not self._stop.is_set():
            self._render()

    def advance(self, label: str) -> None:
        with self._lock:
            self.done[label] += 1

    def show_panel(self, title: str, rows: list[tuple[str, str]]) -> None:
        """Insert a panel between live redraws without leaving copied bars behind."""
        with self._paint_lock:
            self._clear_live()
            content = panel(title, rows)
            if self.vt:
                heading, separator, *details = content.splitlines()
                content = "\n".join([self._paint(heading, "1;92"), self._paint(separator, "32"), *details])
            self._write(content + "\n\n")
            self.stream.flush()
            self._last_signature = None
        if self._thread and not self._stop.is_set():
            self._render()

    def show_round(self, number: int, total: int, passed: int, failed: int,
                   detail: str | None = None) -> None:
        """Keep each completed round on its own line above the live progress."""
        summary = f"  Rodada {number:02d}/{total:02d}  ·  Testes {passed}/{passed + failed}"
        if detail:
            summary += f"  ·  {detail}"
        color = "31" if failed else "32" if detail in (None, "LLM publicado") else "33"
        with self._paint_lock:
            with self._lock:
                self.stage = "Preparando próxima etapa"
                self.totals.clear()
                self.done.clear()
                self.started = time.monotonic()
            self._clear_live()
            self._write(self._paint(summary, color) + "\n")
            self.stream.flush()
            self._last_signature = None

    def _clear_live(self) -> None:
        """Clear only the temporary progress block; leave previous rounds intact."""
        if self.vt and self._lines:
            self._write(f"\x1b[{self._lines}F")
            for _ in range(self._lines):
                self._write("\x1b[2K\n")
            self._write(f"\x1b[{self._lines}F")
            self._lines = 0
        elif self.interactive and self._line_width:
            self._write("\r" + " " * self._line_width + "\r")
            self._line_width = 0

    def _animate(self) -> None:
        while not self._stop.wait(self.interval):
            self._render()

    def _render(self, final: bool = False, ending: str = "Finalizado") -> None:
        with self._paint_lock:
            with self._lock:
                stage = self.stage
                totals = self.totals.copy()
                done = self.done.copy()
                elapsed_seconds = time.monotonic() - self.started
            elapsed = int(elapsed_seconds)
            frame = int(elapsed_seconds / self.interval)
            signature = (stage, tuple(totals.items()), tuple(done.items()), frame, final, ending)
            if signature == self._last_signature:
                return
            self._last_signature = signature
            spinner = "|/-\\"[frame % 4]
            title = ending if final else stage
            header = f"  IDENTITY  {spinner}  {title}  {elapsed // 60:02d}:{elapsed % 60:02d}"
            lines = [self._paint(header, "1;92")]
            if totals:
                lines.append(self._colored_bar("Total", sum(done.values()), sum(totals.values())))
                # A full capability list can exceed the terminal height and make
                # every redraw scroll. Keep recent types visible and aggregate the rest.
                height = shutil.get_terminal_size((88, 24)).lines
                max_lines = min(8, max(4, height - 6))
                slots = max_lines - len(lines)
                if len(totals) > slots:
                    slots -= 1  # reserve a line for the remaining types
                recent = list(reversed(done))
                visible = list(dict.fromkeys([*recent, *totals]))[:slots]
                for label in visible:
                    lines.append(self._colored_bar(label, done[label], totals[label]))
                remaining = [label for label in totals if label not in visible]
                if remaining:
                    lines.append(self._colored_bar(
                        f"Outros ({len(remaining)})",
                        sum(done[label] for label in remaining),
                        sum(totals[label] for label in remaining),
                    ))
            if self.vt:
                if self._lines:
                    self._write(f"\x1b[{self._lines}F")
                for index in range(max(self._lines, len(lines))):
                    self._write("\x1b[2K" + (lines[index] if index < len(lines) else "") + "\n")
                self._lines = max(self._lines, len(lines))
            else:
                line = header + ("  " + self._bar("Total", sum(done.values()), sum(totals.values())) if totals else "")
                self._write("\r" + line.ljust(self._line_width))
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
        columns = shutil.get_terminal_size((88, 24)).columns
        if columns < 42:
            label_width = max(3, min(10, columns - 15))
            bar_width = max(3, columns - label_width - 13)
            filled = round(fraction * bar_width)
            return f"  {label[:label_width]:<{label_width}} [{'#' * filled}{'-' * (bar_width - filled)}] {done}/{total}"
        label_width = min(18, max(8, columns // 4))
        bar_width = max(5, min(22, columns - label_width - 26))
        filled = round(fraction * bar_width)
        return f"  {label[:label_width]:<{label_width}}  [{'#' * filled}{'-' * (bar_width - filled)}]  {done}/{total}  {fraction:4.0%}"
