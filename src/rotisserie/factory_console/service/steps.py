"""How a long service reports each step without knowing who listens."""

from __future__ import annotations

from typing import Protocol


class Step(Protocol):
    """The console's ``step``: a JSON line with ``--progress``, else a live line."""

    def __call__(self, text: str, **fields: object) -> None: ...


def quiet(text: str, **fields: object) -> None:
    return
