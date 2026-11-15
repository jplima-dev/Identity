"""Compact terminal sections that fit narrow Windows consoles."""

from __future__ import annotations

import shutil
import textwrap


def panel(title: str, rows: list[tuple[str, str]], width: int | None = None) -> str:
    columns = width if width is not None else shutil.get_terminal_size((88, 24)).columns
    if columns < 6:
        return "\n".join([title[:columns], *(value[:columns] for _, value in rows)])
    indent = "  " if columns >= 12 else ""
    inner = min(columns - len(indent), 76)
    heading = title.strip()[:inner]
    lines = [indent + heading, indent + "-" * min(len(heading), inner)]
    label_width = min(18, max((len(label) for label, _ in rows), default=0), max(1, inner // 3))
    for label, value in rows:
        available = max(1, inner - label_width - 2)
        parts = textwrap.wrap(str(value), width=available, break_long_words=True, break_on_hyphens=False) or [""]
        for index, part in enumerate(parts):
            name = label[:label_width] if index == 0 else ""
            content = f"{name:<{label_width}}  {part}"
            lines.append(indent + content[:inner].rstrip())
    return "\n".join(lines)
