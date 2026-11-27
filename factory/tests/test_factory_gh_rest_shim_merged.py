"""The REST shim lists merged PRs: one recent page, merged rows only, with mergedAt."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

SHIM = Path(__file__).resolve().parents[1] / ".github/scripts/factory-gh-rest-shim.sh"

ROWS = [
    {"number": 5806, "title": "Fix", "body": "Closes #5525.", "state": "closed",
     "merged_at": "2026-11-26T05:03:29Z", "labels": [{"name": "factory"}],
     "head": {"ref": "factory/work-checkpoints/issue-5525/m/1", "sha": "a"}},
    {"number": 5805, "title": "Failed", "body": "", "state": "closed", "merged_at": None,
     "labels": [{"name": "factory"}], "head": {"ref": "x", "sha": "b"}},
]


def test_merged_pr_list_reads_one_closed_page_and_keeps_only_merged_rows(tmp_path: Path) -> None:
    """In CI the stranded-closure sweep saw no PRs: REST has no state=merged, and mergedAt was lost."""
    log = tmp_path / "calls.txt"
    fake = tmp_path / "gh-real"
    fake.write_text(
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n" "$*" >> {log}\n'
        f"cat {tmp_path / 'page.json'}\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    (tmp_path / "page.json").write_text(json.dumps(ROWS), encoding="utf-8")
    env = {**os.environ, "FACTORY_REAL_GH": str(fake), "GITHUB_REPOSITORY": "acme/widgets"}
    out = subprocess.run(
        ["bash", str(SHIM), "pr", "list", "--repo", "acme/widgets", "--state", "merged",
         "--label", "factory", "--limit", "100", "--json", "number,headRefName,body,mergedAt"],
        text=True, capture_output=True, check=True, env=env,
    )
    rows = json.loads(out.stdout)
    assert [row["number"] for row in rows] == [5806]
    assert rows[0]["mergedAt"] == "2026-11-26T05:03:29Z" and rows[0]["state"] == "MERGED"
    call = log.read_text(encoding="utf-8")
    assert "state=closed&sort=updated&direction=desc&per_page=100" in call
    assert "--paginate" not in call
