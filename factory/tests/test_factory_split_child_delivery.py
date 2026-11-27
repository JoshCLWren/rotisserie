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


def test_the_guard_runs_from_the_trusted_review_stage_alone(tmp_path) -> None:
    """#349 imported factory_split_plan, which the review stage does not copy; every verdict crashed."""
    import os
    import re
    import shutil
    import subprocess
    import sys
    from pathlib import Path

    scripts = Path(delivery.__file__).parent
    primitives = (scripts / "free-model-factory-worker-primitives.sh").read_text(encoding="utf-8")
    block = primitives.split("stage_trusted_review_controller() {", 1)[1].split("\n}\n", 1)[0]
    for name in sorted(set(re.findall(r"\.github/scripts/([\w.-]+\.py)", block))):
        shutil.copy(scripts / name, tmp_path / name)
    body = child_body("docs/api/route-fragments/*.json")
    code = (
        "import json, sys, factory_implementation_delivery as d\n"
        "print(json.dumps(d.pr_delivers_implementation(sys.argv[1], [sys.argv[2]], sys.argv[3])))"
    )
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    out = subprocess.run(
        [sys.executable, "-c", code, body, FRAGMENT, PATCH],
        cwd=tmp_path, text=True, capture_output=True, env=env,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip().startswith("[true")


def test_the_diff_builder_gives_named_docs_files_full_context() -> None:
    """#5883, #5917 and #5936 changed only fragment JSON; partial hunks read as no code change."""
    import json

    head = "a" * 40
    old = '{\n  "path": "/api/v2/activity",\n  "dest": "/api/native_roll.rs"\n}'
    new = old.replace("native_roll", "native_api")

    def read(args: list[str]) -> str:
        endpoint = args[-1]
        if endpoint.endswith("/pulls/5936"):
            return json.dumps({"head": {"sha": head}, "base": {"sha": "base"}})
        if "/compare/" in endpoint:
            return json.dumps({"merge_base_commit": {"sha": "ancestor"}})
        if endpoint.endswith("/files"):
            return json.dumps([[{"filename": FRAGMENT, "status": "modified", "patch":
                                 '@@ -3 +3 @@\n-  "dest": "/api/native_roll.rs"\n'
                                 '+  "dest": "/api/native_api.rs"'}]])
        return old if endpoint.endswith("ref=ancestor") else new

    body = child_body("docs/api/route-fragments/*.json")
    paths, diff = delivery.complete_pr_diff(5936, head, "o/r", read, body)
    assert delivery.pr_delivers_implementation(body, paths, diff)[0]
    plain_paths, plain_diff = delivery.complete_pr_diff(5936, head, "o/r", read)
    assert not delivery.pr_delivers_implementation(body, plain_paths, plain_diff)[0]
