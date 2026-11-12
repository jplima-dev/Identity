"""Schedule follow-up rounds without discarding failed tests."""

from __future__ import annotations


def next_cases[T](pool: list[T], assignments: list[T], passed: list[bool], cursor: int) -> tuple[list[T], int, bool]:
    """Keep failed cases; replace each passing case with a different pool item."""
    if len(assignments) != len(passed):
        raise ValueError("Resultados e agentes têm tamanhos diferentes.")
    updated = assignments.copy()
    changed = False
    for index, success in enumerate(passed):
        if not success:
            continue
        for _ in range(len(pool)):
            candidate = pool[cursor % len(pool)]
            cursor += 1
            if candidate != assignments[index]:
                updated[index] = candidate
                changed = True
                break
    return updated, cursor, changed
