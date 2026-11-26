"""A split child's declared files count as its delivery, also under docs/."""

from __future__ import annotations

import factory_implementation_delivery as delivery
import factory_split_plan as plans

from tests.test_factory_split_plan import PARENT


def child_body(*files: str) -> str:
    row = {
        "title": "Point route fragments at native_api",
        "body": "Change the route fragments so they name native_api.",
        "acceptance": ["Every fragment names native_api"],
        "files": list(files),
        "blocked_by": [],
    }
    return plans.compose(PARENT, 3, row, {})


FRAGMENT = "docs/api/route-fragments/roll.json"
PATCH = (
    f"diff --git a/{FRAGMENT} b/{FRAGMENT}\n--- a/{FRAGMENT}\n+++ b/{FRAGMENT}\n"
    '@@ -1 +1 @@\n-{"destination": "/api/native_roll"}\n+{"destination": "/api/native_api"}\n'
)


def test_a_split_child_delivers_the_docs_files_it_names() -> None:
    """ComicPile #5880 changed only route fragments and was refused as no production change."""
    body = child_body("docs/api/route-fragments/*.json", "scripts/generate_route_burndown.mjs")
    assert plans.declared_files(body) == [
        "docs/api/route-fragments/*.json", "scripts/generate_route_burndown.mjs"
    ]
    delivered, reason = delivery.pr_delivers_implementation(body, [FRAGMENT], PATCH)
    assert delivered, reason


def test_an_ordinary_issue_or_unnamed_docs_file_still_needs_production_source() -> None:
    plain = "Fix the roll route.\n\n## Files\n\n- `docs/api/route-fragments/*.json`\n"
    assert plans.declared_files(plain) == []
    assert not delivery.pr_delivers_implementation(plain, [FRAGMENT], PATCH)[0]
    other = child_body("scripts/generate_route_burndown.mjs")
    assert not delivery.pr_delivers_implementation(other, [FRAGMENT], PATCH)[0]
    tests = child_body("tests/*.py")
    assert not delivery.pr_delivers_implementation(tests, ["tests/test_roll.py"], PATCH)[0]
