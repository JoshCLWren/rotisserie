#!/usr/bin/env python3
"""Deterministically reconcile merged pull requests into the release ledger."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

_GITHUB_API_BASE = "https://api.github.com"
_RATE_LIMIT_PATTERN = re.compile(
    r"Too Many Requests|status.?429|HTTP.?429|rate.?limit",
    re.IGNORECASE,
)
_CONFLICT_PATTERN = re.compile(r"HTTP 409", re.IGNORECASE)


@dataclass(frozen=True)
class Source:
    """Exact merged pull-request identity."""

    number: int
    merged_at: str
    merge_commit_sha: str
    title: str


@dataclass(frozen=True)
class CheckResult:
    """Durable release-ledger lookup result."""

    exists: bool
    release: dict[str, object] | None


class SourceConflictError(RuntimeError):
    """Raised when the ledger reports conflicting source provenance."""


def _repository_parts(repository: str) -> tuple[str, str]:
    parts = repository.split("/")
    if len(parts) != 2 or not all(parts):
        raise ValueError("repository must use owner/name form")
    return (
        urllib.parse.quote(parts[0], safe=""),
        urllib.parse.quote(parts[1], safe=""),
    )


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return parsed.astimezone(UTC)


def _github_request(url: str) -> object:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "ComicPile-release-reconcile",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.getenv("GH_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        raise RuntimeError(
            f"GitHub API returned HTTP {exc.code}: {detail[:1000]}"
        ) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"GitHub API request failed: {exc.reason}") from exc


def _source_from_pull(item: object) -> Source | None:
    if not isinstance(item, dict):
        return None
    number = item.get("number")
    merged_at = item.get("merged_at")
    merge_sha = item.get("merge_commit_sha")
    title = item.get("title")
    if (
        isinstance(number, int)
        and isinstance(merged_at, str)
        and isinstance(merge_sha, str)
        and isinstance(title, str)
    ):
        return Source(
            number=number,
            merged_at=merged_at,
            merge_commit_sha=merge_sha,
            title=title,
        )
    return None


def _fetch_one(repository: str, number: int) -> Source:
    owner, name = _repository_parts(repository)
    result = _github_request(
        f"{_GITHUB_API_BASE}/repos/{owner}/{name}/pulls/{number}"
    )
    source = _source_from_pull(result)
    if source is None:
        raise RuntimeError(f"PR #{number} is not merged to main")
    if isinstance(result, dict):
        base = result.get("base")
        if isinstance(base, dict) and base.get("ref") != "main":
            raise RuntimeError(f"PR #{number} was not merged to main")
    return source


def _discover(
    repository: str,
    *,
    limit: int,
    since: str | None,
) -> list[Source]:
    if limit < 1:
        raise ValueError("limit must be positive")
    since_dt = _parse_timestamp(since) if since else None
    owner, name = _repository_parts(repository)
    merged: list[Source] = []
    page = 1
    while page <= 100:
        query = urllib.parse.urlencode(
            {
                "state": "closed",
                "base": "main",
                "sort": "updated",
                "direction": "desc",
                "per_page": 100,
                "page": page,
            }
        )
        result = _github_request(
            f"{_GITHUB_API_BASE}/repos/{owner}/{name}/pulls?{query}"
        )
        if not isinstance(result, list):
            raise RuntimeError("GitHub pulls response must be a list")
        if not result:
            break

        last_updated: datetime | None = None
        for item in result:
            source = _source_from_pull(item)
            if source is not None:
                merged_dt = _parse_timestamp(source.merged_at)
                if since_dt is None or merged_dt > since_dt:
                    merged.append(source)
            if isinstance(item, dict) and isinstance(item.get("updated_at"), str):
                last_updated = _parse_timestamp(str(item["updated_at"]))

        merged.sort(key=lambda item: _parse_timestamp(item.merged_at), reverse=True)
        if since_dt is not None:
            if last_updated is not None and last_updated <= since_dt:
                break
        elif len(merged) >= limit and last_updated is not None:
            cutoff = _parse_timestamp(merged[limit - 1].merged_at)
            if last_updated <= cutoff:
                break

        if len(result) < 100:
            break
        page += 1

    if page > 100:
        raise RuntimeError("GitHub pull pagination exceeded safety bound")
    merged.sort(key=lambda item: _parse_timestamp(item.merged_at))
    if since_dt is not None:
        return merged
    return merged[-limit:]


def _run_helper(arguments: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "scripts/release_writer.py", *arguments],
        check=False,
        capture_output=True,
        text=True,
    )


def _check_source(repository: str, source: Source) -> CheckResult:
    result = _run_helper(
        (
            "check",
            repository,
            str(source.number),
            source.merge_commit_sha,
        )
    )
    if result.returncode != 0:
        detail = "\n".join(part for part in (result.stdout, result.stderr) if part)
        if _CONFLICT_PATTERN.search(detail):
            raise SourceConflictError(detail.strip())
        raise RuntimeError(
            f"release-ledger check failed for PR #{source.number}: {detail.strip()}"
        )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"release-ledger check returned invalid JSON for PR #{source.number}"
        ) from exc
    if not isinstance(payload, dict):
        raise RuntimeError("release-ledger check response must be an object")
    release = payload.get("release")
    return CheckResult(
        exists=payload.get("exists") is True,
        release=release if isinstance(release, dict) else None,
    )


def _outcome_from_release(release: dict[str, object] | None) -> str:
    staged = bool(os.getenv("RELEASE_WRITER_STAGE_FILE"))
    internal = release is not None and release.get("visibility") == "internal"
    if staged:
        return "staged_internal" if internal else "staged_public"
    return "newly_skipped_internal" if internal else "newly_published"


def _prompt(repository: str, source: Source) -> str:
    if os.getenv("RELEASE_WRITER_STAGE_FILE"):
        return (
            f"Prepare exactly one merged PR in {repository}: #{source.number} "
            f"({source.title}), merge_sha={source.merge_commit_sha}, "
            f"merged_at={source.merged_at}. Check its exact staged source, "
            "inspect PR/files/issues through release_writer.py, then stage "
            "exactly one validated public release or internal skip using "
            "that helper. Check the staged source again before exiting. "
            "Staging is NOT durable publication: the separate trusted job "
            "validates and writes it directly to Neon. Do not process other PRs."
        )
    return (
        f"Process exactly one merged pull request in {repository}: "
        f"#{source.number} ({source.title}). Exact source identity: "
        f"merge_sha={source.merge_commit_sha}, merged_at={source.merged_at}. "
        "Check this exact source in the release ledger first. If it is missing, "
        "inspect the PR with the release_writer.py helper and create exactly one "
        "durable outcome: publish a public release or record an internal skip. "
        "Before exiting, run the exact check again. Do not process any other PR."
    )


def _run_model(
    repository: str,
    source: Source,
    model: str,
) -> subprocess.CompletedProcess[str]:
    workspace = os.getenv("GITHUB_WORKSPACE", os.getcwd())
    command = [
        "opencode",
        "run",
        "-m",
        model,
        "--agent",
        "release-writer",
        "--auto",
        "--title",
        f"ComicPile release writer PR #{source.number}",
        _prompt(repository, source),
    ]
    result = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        cwd=workspace,  # OpenCode 2 rejects `opencode run --dir`.
    )
    output = "\n".join(part for part in (result.stdout, result.stderr) if part)
    if output:
        print(output)
    return result


def _model_list(raw: str) -> list[str]:
    return [item.strip() for item in raw.splitlines() if item.strip()]


def _process_source(
    repository: str,
    source: Source,
    models: Sequence[str],
) -> dict[str, object]:
    before = _check_source(repository, source)
    if before.exists:
        return {
            "pr": source.number,
            "status": "already_present",
            "merge_sha": source.merge_commit_sha,
        }

    if not models:
        return {
            "pr": source.number,
            "status": "failed",
            "merge_sha": source.merge_commit_sha,
            "reason": "no healthy release-writer models were available",
        }

    false_green_models: list[str] = []
    rate_limited_models: list[str] = []
    for model in models:
        print(f"::notice::Reconciling PR #{source.number} with {model}")
        attempt = _run_model(repository, source, model)
        after = _check_source(repository, source)
        if after.exists:
            return {
                "pr": source.number,
                "status": _outcome_from_release(after.release),
                "merge_sha": source.merge_commit_sha,
                "model": model,
            }

        combined = "\n".join(
            part for part in (attempt.stdout, attempt.stderr) if part
        )
        if attempt.returncode == 0:
            false_green_models.append(model)
            print(
                f"::warning::Model {model} exited 0 for PR #{source.number} "
                "without creating a durable release-ledger source."
            )
            continue
        if _RATE_LIMIT_PATTERN.search(combined):
            rate_limited_models.append(model)
            print(
                f"::warning::Model {model} was rate-limited for "
                f"PR #{source.number}; trying the next model."
            )
            continue
        return {
            "pr": source.number,
            "status": "failed",
            "merge_sha": source.merge_commit_sha,
            "model": model,
            "reason": (
                f"release writer exited {attempt.returncode} without a durable "
                "source record"
            ),
        }

    reason_parts = []
    if false_green_models:
        reason_parts.append(
            "false-green models: " + ", ".join(false_green_models)
        )
    if rate_limited_models:
        reason_parts.append(
            "rate-limited models: " + ", ".join(rate_limited_models)
        )
    return {
        "pr": source.number,
        "status": "failed",
        "merge_sha": source.merge_commit_sha,
        "reason": "; ".join(reason_parts) or "all model attempts were exhausted",
    }


def _summary(
    repository: str,
    results: Sequence[dict[str, object]],
    *,
    conflict: bool,
    unexamined: int,
) -> dict[str, object]:
    statuses = [str(item.get("status", "")) for item in results]
    failures = statuses.count("failed")
    conflicts = statuses.count("conflict")
    return {
        "repository": repository,
        "total_merged_prs_examined": len(results),
        "already_present": statuses.count("already_present"),
        "newly_published": statuses.count("newly_published"),
        "newly_skipped_internal": statuses.count("newly_skipped_internal"),
        "staged_public": statuses.count("staged_public"),
        "staged_internal": statuses.count("staged_internal"),
        "conflicts": conflicts,
        "failures": failures,
        "still_missing": failures,
        "stopped_on_conflict": conflict,
        "unexamined_after_conflict": unexamined,
        "results": list(results),
    }


def _write_summary(
    summary: dict[str, object],
    json_path: str | None,
    markdown_path: str | None,
) -> None:
    rendered = json.dumps(summary, indent=2, sort_keys=True)
    print(rendered)
    if json_path:
        Path(json_path).write_text(rendered + "\n", encoding="utf-8")
    if markdown_path:
        lines = [
            "## Release reconciliation",
            "",
            f"- Examined: {summary['total_merged_prs_examined']}",
            f"- Already present: {summary['already_present']}",
            f"- Newly published: {summary['newly_published']}",
            f"- Newly skipped/internal: {summary['newly_skipped_internal']}",
            f"- Staged public (not yet durable): {summary['staged_public']}",
            f"- Staged internal (not yet durable): {summary['staged_internal']}",
            f"- Conflicts: {summary['conflicts']}",
            f"- Failures: {summary['failures']}",
            f"- Still missing: {summary['still_missing']}",
        ]
        unexamined = int(summary["unexamined_after_conflict"])
        if unexamined:
            lines.append(f"- Unexamined after conflict: {unexamined}")
        Path(markdown_path).write_text(
            "\n".join(lines) + "\n",
            encoding="utf-8",
        )


def reconcile(
    repository: str,
    sources: Sequence[Source],
    models: Sequence[str],
) -> tuple[list[dict[str, object]], bool, int]:
    """Reconcile sources independently, stopping only on provenance conflict."""
    results: list[dict[str, object]] = []
    for index, source in enumerate(sources):
        try:
            results.append(_process_source(repository, source, models))
        except SourceConflictError as exc:
            results.append(
                {
                    "pr": source.number,
                    "status": "conflict",
                    "merge_sha": source.merge_commit_sha,
                    "reason": str(exc),
                }
            )
            return results, True, len(sources) - index - 1
        except RuntimeError as exc:
            results.append(
                {
                    "pr": source.number,
                    "status": "failed",
                    "merge_sha": source.merge_commit_sha,
                    "reason": str(exc),
                }
            )
    return results, False, 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--repository",
        default=os.getenv("GITHUB_REPOSITORY", ""),
    )
    parser.add_argument("--pr", type=int)
    parser.add_argument("--since")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument(
        "--models",
        default=os.getenv("RELEASE_WRITER_MODELS", ""),
    )
    parser.add_argument("--summary-json")
    parser.add_argument("--summary-markdown")
    return parser


def main() -> int:
    """Run deterministic release reconciliation."""
    args = _parser().parse_args()
    if not args.repository:
        raise SystemExit("--repository or GITHUB_REPOSITORY is required")
    if args.pr is not None and args.since is not None:
        raise SystemExit("--pr and --since are mutually exclusive")
    try:
        if args.pr is not None:
            sources = [_fetch_one(args.repository, args.pr)]
        else:
            sources = _discover(
                args.repository,
                limit=args.limit,
                since=args.since,
            )
        results, conflict, unexamined = reconcile(
            args.repository,
            sources,
            _model_list(args.models),
        )
    except (RuntimeError, ValueError) as exc:
        print(f"release reconciliation failed: {exc}", file=sys.stderr)
        return 1

    summary = _summary(
        args.repository,
        results,
        conflict=conflict,
        unexamined=unexamined,
    )
    _write_summary(summary, args.summary_json, args.summary_markdown)
    return 1 if summary["conflicts"] or summary["failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
