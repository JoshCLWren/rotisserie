"""The console stops showing a park once an owner-directed run released it."""

from __future__ import annotations

from typing import Any

from test_factory_item_loops import (  # type: ignore[import-not-found]
    AT_0130,
    TITLE,
    Hub,
    park_comment,
    pull,
)

from rotisserie.factory_console import runtime
from rotisserie.factory_console.service import loops

HEAD = "d" * 40


def release_comment(head: str) -> dict[str, Any]:
    rules = runtime.module("factory_self_heal_rules")
    body = rules.comment_body(
        kind=rules.LOOP_RELEASED, pr=5766, head=head, ref="run-1", text="Released."
    )
    return {"user": {"login": "github-actions[bot]", "type": "Bot"}, "body": body}


def test_a_released_park_is_not_listed_and_a_new_park_is() -> None:
    held = {**pull(5766, TITLE), "head": {"sha": HEAD}}
    hub = Hub([park_comment(HEAD, "review"), release_comment(HEAD)])
    assert loops.parked(hub, now=AT_0130, pulls=[held]) == []
    hub.comments.append(park_comment(HEAD, "review"))
    rows = loops.parked(hub, now=AT_0130, pulls=[held])
    assert [row["number"] for row in rows] == [5766]
