"""Regression tests for safe Vercel Container Registry pruning."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / ".github" / "scripts" / "prune-oldest-vcr-image.sh"

# Git exports these into hook environments (e.g. the pre-push hook). When a test
# subprocess inherits them, git targets the outer repository instead of the
# fixture's throwaway repo, so strip every git override before spawning git.
_GIT_OVERRIDE_VARS = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_NAMESPACE",
    "GIT_CEILING_DIRECTORIES",
    "GIT_DISCOVERY_ACROSS_FILESYSTEM",
    "GIT_CONFIG_GLOBAL",
    "GIT_CONFIG_SYSTEM",
    "GIT_COMMON_DIR",
)


def _git_safe_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k not in _GIT_OVERRIDE_VARS}


def _write_mock_tools(
    tmp_path: Path,
    tags: list[str],
    digests: dict[str, str],
    created_by_tag: dict[str, str | None] | None = None,
) -> tuple[Path, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    deletion_log = tmp_path / "deleted.txt"
    metadata = tmp_path / "metadata.json"
    metadata.write_text(
        json.dumps(
            {
                "tags": tags,
                "digests": digests,
                "created_by_tag": created_by_tag or {},
            }
        ),
        encoding="utf-8",
    )

    oras = bin_dir / "oras"
    oras.write_text(
        """#!/usr/bin/env python3
import json
import os
import pathlib
import sys

args = sys.argv[1:]
metadata = json.loads(pathlib.Path(os.environ["MOCK_METADATA"]).read_text())
if args[0] == "login":
    raise SystemExit(0)
if args[:2] == ["repo", "tags"]:
    print(json.dumps({"tags": metadata["tags"]}))
    raise SystemExit(0)
if args[0] == "resolve":
    tag = args[-1].rsplit(":", 1)[-1]
    print(metadata["digests"][tag])
    raise SystemExit(0)
if args[:2] == ["manifest", "fetch-config"]:
    tag = args[-1].rsplit(":", 1)[-1]
    if tag in metadata["created_by_tag"]:
        created = metadata["created_by_tag"][tag]
        print(json.dumps({} if created is None else {"created": created}))
    else:
        day = metadata["tags"].index(tag) + 1
        print(json.dumps({"created": f"2026-01-{day:02d}T00:00:00Z"}))
    raise SystemExit(0)
if args[:2] == ["manifest", "delete"]:
    pathlib.Path(os.environ["DELETION_LOG"]).write_text(args[-1])
    raise SystemExit(0)
raise SystemExit(f"unexpected oras arguments: {args}")
""",
        encoding="utf-8",
    )
    oras.chmod(0o755)

    jq = bin_dir / "jq"
    jq.write_text(
        """#!/usr/bin/env python3
import json
import sys

data = json.load(sys.stdin)
query = sys.argv[-1]
if query == ".tags[]?":
    for tag in data.get("tags", []):
        print(tag)
elif query == ".created // empty":
    created = data.get("created")
    if created:
        print(created)
else:
    raise SystemExit(f"unexpected jq query: {query}")
""",
        encoding="utf-8",
    )
    jq.chmod(0o755)
    return bin_dir, deletion_log


def _run_pruner(
    tmp_path: Path,
    tags: list[str],
    digests: dict[str, str],
    keep_tags: str = "",
    created_by_tag: dict[str, str | None] | None = None,
    cwd: Path | None = None,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    bin_dir, deletion_log = _write_mock_tools(
        tmp_path,
        tags,
        digests,
        created_by_tag,
    )
    env = {
        **_git_safe_env(),
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "ORAS_BIN": str(bin_dir / "oras"),
        "JQ_BIN": str(bin_dir / "jq"),
        "GIT_BIN": "git",
        "MOCK_METADATA": str(tmp_path / "metadata.json"),
        "DELETION_LOG": str(deletion_log),
        "VCR_IMAGE": "vcr.vercel.com/team/project/dockerfile",
        "VERCEL_TOKEN": "test-token",
        "VERCEL_TEAM_ID": "team_test",
        "VCR_KEEP_TAGS": keep_tags,
    }
    result = subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=cwd or tmp_path,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    return result, deletion_log


def _create_git_commit(repo: Path, timestamp: str) -> str:
    repo.mkdir()
    git_env = _git_safe_env()
    subprocess.run(["git", "init", "--quiet"], cwd=repo, env=git_env, check=True)
    subprocess.run(
        ["git", "config", "user.email", "profile-test@example.com"],
        cwd=repo,
        env=git_env,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Production Profile Test"],
        cwd=repo,
        env=git_env,
        check=True,
    )
    (repo / "fixture.txt").write_text("fixture\n", encoding="utf-8")
    subprocess.run(["git", "add", "fixture.txt"], cwd=repo, env=git_env, check=True)
    commit_env = {
        **git_env,
        "GIT_AUTHOR_DATE": timestamp,
        "GIT_COMMITTER_DATE": timestamp,
    }
    subprocess.run(
        ["git", "commit", "--quiet", "-m", "fixture"],
        cwd=repo,
        env=commit_env,
        check=True,
    )
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        env=git_env,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_pruner_deletes_oldest_unprotected_manifest(tmp_path: Path) -> None:
    """Delete the oldest unique manifest that is not protected."""
    tags = ["old", "middle", "live"]
    digests = {"old": "sha256:old", "middle": "sha256:middle", "live": "sha256:live"}

    result, deletion_log = _run_pruner(tmp_path, tags, digests, keep_tags="live")

    assert result.returncode == 0, result.stderr
    assert deletion_log.read_text() == "vcr.vercel.com/team/project/dockerfile@sha256:old"
    assert "Protecting VCR image live (sha256:live)" in result.stdout


def test_pruner_protects_all_aliases_of_kept_digest(tmp_path: Path) -> None:
    """Protect every tag alias that resolves to a kept digest."""
    tags = ["live-alias", "live", "old"]
    digests = {
        "live-alias": "sha256:live",
        "live": "sha256:live",
        "old": "sha256:old",
    }

    result, deletion_log = _run_pruner(tmp_path, tags, digests, keep_tags="live")

    assert result.returncode == 0, result.stderr
    assert deletion_log.read_text() == "vcr.vercel.com/team/project/dockerfile@sha256:old"


def test_pruner_retries_digest_alias_with_git_commit_timestamp(tmp_path: Path) -> None:
    """Retry a digest through a Git SHA alias after metadata lookup fails."""
    repo = tmp_path / "repo"
    commit_sha = _create_git_commit(repo, "2025-01-01T00:00:00Z")
    tags = ["missing-created", commit_sha, "newer"]
    digests = {
        "missing-created": "sha256:old",
        commit_sha: "sha256:old",
        "newer": "sha256:newer",
    }

    result, deletion_log = _run_pruner(
        tmp_path,
        tags,
        digests,
        created_by_tag={
            "missing-created": None,
            "newer": "2026-01-03T00:00:00Z",
        },
        cwd=repo,
    )

    assert result.returncode == 0, result.stderr
    assert "Skipping missing-created" in result.stdout
    assert deletion_log.read_text() == "vcr.vercel.com/team/project/dockerfile@sha256:old"


def test_pruner_keeps_the_only_unique_manifest(tmp_path: Path) -> None:
    """Keep the repository intact when only one unique manifest exists."""
    tags = ["alias-a", "alias-b"]
    digests = {"alias-a": "sha256:only", "alias-b": "sha256:only"}

    result, deletion_log = _run_pruner(tmp_path, tags, digests)

    assert result.returncode == 0, result.stderr
    assert not deletion_log.exists()
    assert "keeping the only deployable image" in result.stdout
