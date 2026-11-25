#!/usr/bin/env python3
"""Line-anchored review findings: parse, validate against the head diff, decide.

The owner's rule (2026-11-25): a review is its findings. The reviewer prints one
``FACTORY_REVIEW_FINDINGS_V1=`` record. Each finding names a path and a line (or
range) on the right side of the PR's exact-head diff, a severity and the fix.
Findings outside the diff are dropped and counted. Zero valid blocking findings
approves; one or more requests changes; no parseable record is a failed run.
Nothing here reads the network.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from factory_adopter import MARKER_PREFIX  # noqa: E402

PREFIX = "FACTORY_REVIEW_FINDINGS_V1="
BLOCKING = "blocking"
NON_BLOCKING = "non-blocking"
SEVERITIES = (BLOCKING, NON_BLOCKING)
MAX_FINDINGS = 25
MAX_BODY = 1500
APPROVE, REPAIR, REJECT, FAILED = "approve", "repair", "reject", "failed"
HEAD_RE = re.compile(r"[0-9a-f]{40}")
HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(?P<start>\d+)(?:,(?P<count>\d+))? @@")
FINDING_MARKER = f"{MARKER_PREFIX}-factory-review-finding-v1"
SUMMARY_MARKER = f"{MARKER_PREFIX}-factory-review-findings-v1"
FINDING_MARKER_RE = re.compile(
    rf"^<!-- {re.escape(FINDING_MARKER)}:pr-(?P<pr>\d+):head-(?P<head>[0-9a-f]{{40}}):"
    rf"severity-(?P<severity>blocking|non-blocking):lines-(?P<start>\d+)-(?P<end>\d+) -->"
)
SUMMARY_MARKER_RE = re.compile(
    rf"^<!-- {re.escape(SUMMARY_MARKER)}:pr-(?P<pr>\d+):head-(?P<head>[0-9a-f]{{40}}):"
    rf"verdict-(?P<verdict>[a-z]+):blocking-(?P<blocking>\d+):valid-(?P<valid>\d+):"
    rf"dropped-(?P<dropped>\d+) -->"
)
RANKS = {APPROVE: 0, REPAIR: 1, REJECT: 2}


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    severity: str
    body: str
    start_line: int | None = None

    @property
    def first(self) -> int:
        return self.start_line or self.line

    def location(self) -> str:
        span = f"{self.start_line}-{self.line}" if self.start_line else str(self.line)
        return f"{self.path}:{span}"


@dataclass(frozen=True)
class Dropped:
    index: int
    location: str
    reason: str


@dataclass(frozen=True)
class Review:
    """The validated result of one review log against one head diff."""

    parsed: bool
    error: str = ""
    valid: tuple[Finding, ...] = ()
    dropped: tuple[Dropped, ...] = ()

    @property
    def blocking(self) -> tuple[Finding, ...]:
        return tuple(item for item in self.valid if item.severity == BLOCKING)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["blocking"] = len(self.blocking)
        return data


@dataclass(frozen=True)
class Record:
    head: str
    items: tuple[object, ...] = field(default=())


def _record_texts(text: str) -> list[str]:
    """Record lines at column zero, from plain logs or OpenCode JSONL text events."""
    found: list[str] = []
    for raw in text.splitlines():
        if raw.startswith(PREFIX):
            found.append(raw[len(PREFIX) :])
            continue
        if not raw.startswith("{"):
            continue
        try:
            event = json.loads(raw)
        except ValueError:
            continue
        part = (
            event.get("part") if isinstance(event, dict) and event.get("type") == "text" else None
        )
        message = (
            part.get("text") if isinstance(part, dict) and part.get("type") == "text" else None
        )
        if isinstance(message, str):
            found += [
                line[len(PREFIX) :] for line in message.splitlines() if line.startswith(PREFIX)
            ]
    return found


def parse_record(text: str, *, head: str) -> tuple[Record | None, str]:
    """The single findings record for this head, or why there is none."""
    records: list[dict[str, Any]] = []
    for line in _record_texts(text):
        try:
            data = json.loads(line)
        except ValueError:
            return None, "malformed findings record"
        if not isinstance(data, dict):
            return None, "findings record is not a JSON object"
        if data not in records:
            records.append(data)
    if not records:
        return None, "no findings record"
    if len(records) > 1:
        return None, "more than one different findings record"
    data = records[0]
    if data.get("head") != head:
        return None, f"findings record names head {str(data.get('head'))[:40]!r}, not {head}"
    items = data.get("findings")
    if not isinstance(items, list):
        return None, "findings record has no findings list"
    return Record(head=head, items=tuple(items)), ""


def right_lines(patch: str) -> list[range]:
    """Right-side (head) line ranges of each hunk in one file's unified patch."""
    hunks: list[range] = []
    for line in patch.splitlines():
        match = HUNK_RE.match(line)
        if match:
            start = int(match.group("start"))
            count = int(match.group("count") or 1)
            if count:
                hunks.append(range(start, start + count))
    return hunks


def diff_index(files: Iterable[Mapping[str, Any]]) -> dict[str, list[range]]:
    """Path to right-side hunk ranges, from a pulls/N/files listing."""
    index: dict[str, list[range]] = {}
    for item in files:
        path = item.get("filename")
        if isinstance(path, str) and path:
            patch = item.get("patch")
            index[path] = right_lines(patch) if isinstance(patch, str) else []
    return index


def clean_body(text: str) -> str:
    """Untrusted model prose made safe to post as the factory."""
    text = text.replace("<!--", "").replace("-->", "").replace("@", "@​").strip()
    return text if len(text) <= MAX_BODY else text[: MAX_BODY - 3].rstrip() + "..."


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _finding(item: object, index: Mapping[str, list[range]]) -> tuple[Finding | None, str, str]:
    if not isinstance(item, Mapping):
        return None, "?", "finding is not an object"
    path = item.get("path")
    line = _int(item.get("line"))
    start = _int(item.get("start_line"))
    where = f"{path}:{item.get('line')}"
    if not isinstance(path, str) or not path:
        return None, where, "no path"
    if line is None:
        return None, where, "no positive line"
    severity = item.get("severity")
    if severity not in SEVERITIES:
        return None, where, "severity must be blocking or non-blocking"
    side = item.get("side", "RIGHT")
    if side != "RIGHT":
        return None, where, "only the head (RIGHT) side can be commented"
    body = item.get("body")
    fix = item.get("fix")
    text = " ".join(
        str(part).strip() for part in (body, fix) if isinstance(part, str) and part.strip()
    )
    if not text:
        return None, where, "no explanation"
    if start is not None and start > line:
        return None, where, "start_line is after line"
    start = None if start == line else start
    if path not in index:
        return None, where, "path is not changed by this PR"
    hunk = next((span for span in index[path] if line in span), None)
    if hunk is None:
        return None, where, "line is outside every diff hunk of the head"
    if start is not None and start not in hunk:
        return None, where, "range crosses a diff hunk boundary"
    return (
        Finding(
            path=path, line=line, severity=str(severity), body=clean_body(text), start_line=start
        ),
        where,
        "",
    )


def validate(record: Record | None, error: str, files: Iterable[Mapping[str, Any]]) -> Review:
    """Keep findings anchored inside the head diff; drop and count the rest."""
    if record is None:
        return Review(parsed=False, error=error or "no findings record")
    index = diff_index(files)
    valid: list[Finding] = []
    dropped: list[Dropped] = []
    for number, item in enumerate(record.items, start=1):
        if number > MAX_FINDINGS:
            dropped.append(Dropped(number, "?", f"more than {MAX_FINDINGS} findings"))
            continue
        finding, where, reason = _finding(item, index)
        if finding is None:
            dropped.append(Dropped(number, where, reason))
        elif finding not in valid:
            valid.append(finding)
    return Review(parsed=True, valid=tuple(valid), dropped=tuple(dropped))


def decide(review: Review, *, rejects: bool = False) -> str:
    """The mechanical verdict. The reviewer's own approve or reject prose never decides."""
    if not review.parsed:
        return FAILED
    if not review.blocking:
        return APPROVE
    return REJECT if rejects else REPAIR


def stricter(first: str, second: str) -> str:
    return first if RANKS.get(first, 1) >= RANKS.get(second, 1) else second


def finding_marker(*, pr: int, head: str, finding: Finding) -> str:
    return (
        f"<!-- {FINDING_MARKER}:pr-{pr}:head-{head}:severity-{finding.severity}:"
        f"lines-{finding.first}-{finding.line} -->"
    )


def summary_marker(*, pr: int, head: str, verdict: str, review: Review) -> str:
    return (
        f"<!-- {SUMMARY_MARKER}:pr-{pr}:head-{head}:verdict-{verdict}:"
        f"blocking-{len(review.blocking)}:valid-{len(review.valid)}:"
        f"dropped-{len(review.dropped)} -->"
    )


def summary_body(*, pr: int, head: str, reviewer: str, verdict: str, review: Review) -> str:
    """The review body that carries the inline comments."""
    lines = [
        summary_marker(pr=pr, head=head, verdict=verdict, review=review),
        "",
        f"### Factory code review · Factory {reviewer} · {verdict.upper()}",
        "",
        f"Head `{head[:12]}`: {len(review.blocking)} blocking, "
        f"{len(review.valid) - len(review.blocking)} non-blocking, "
        f"{len(review.dropped)} dropped (not on a changed line of this head).",
    ]
    if review.valid:
        lines.append("")
        lines += [f"- **{item.severity}** `{item.location()}`" for item in review.valid]
    if review.dropped:
        lines += ["", "<details><summary>Dropped findings</summary>", ""]
        lines += [f"- {clean_body(item.location)}: {item.reason}" for item in review.dropped]
        lines += ["", "</details>"]
    return "\n".join(lines)


def comment_payload(*, pr: int, head: str, finding: Finding) -> dict[str, Any]:
    label = "Blocking" if finding.severity == BLOCKING else "Non-blocking"
    comment: dict[str, Any] = {
        "path": finding.path,
        "line": finding.line,
        "side": "RIGHT",
        "body": f"{finding_marker(pr=pr, head=head, finding=finding)}\n**{label}:** {finding.body}",
    }
    if finding.start_line:
        comment.update(start_line=finding.start_line, start_side="RIGHT")
    return comment


def review_payload(
    *, pr: int, head: str, reviewer: str, verdict: str, review: Review
) -> dict[str, Any]:
    """POST body for pulls/N/reviews: anchored by path, line and side, never position."""
    return {
        "commit_id": head,
        "event": "COMMENT",
        "body": summary_body(pr=pr, head=head, reviewer=reviewer, verdict=verdict, review=review),
        "comments": [comment_payload(pr=pr, head=head, finding=item) for item in review.valid],
    }


def unanchored(
    posted: Sequence[Mapping[str, Any]], expected: Sequence[Mapping[str, Any]]
) -> list[str]:
    """Expected comments that GitHub did not anchor to their requested line."""
    have = {(row.get("path"), row.get("line")) for row in posted if row.get("line")}
    return [
        f"{row['path']}:{row['line']}" for row in expected if (row["path"], row["line"]) not in have
    ]


def handoff_text(review: Review) -> str:
    """The repair task: every valid finding with its file, line and fix."""
    if not review.valid:
        return ""
    rows = [f"- [{item.severity}] {item.location()}: {item.body}" for item in review.valid]
    return "Line-anchored review findings to fix:\n" + "\n".join(rows)


def thread_finding(thread: Mapping[str, Any], *, pr: int) -> dict[str, str] | None:
    """The marker of an unresolved factory finding thread on this PR, else None."""
    if thread.get("isResolved"):
        return None
    page = thread.get("comments")
    nodes = page.get("nodes") if isinstance(page, Mapping) else None
    first = nodes[0] if isinstance(nodes, list) and nodes else None
    if not isinstance(first, Mapping):
        return None
    author = first.get("author")
    if not isinstance(author, Mapping) or author.get("__typename") != "Bot":
        return None
    if author.get("login") not in ("github-actions", "github-actions[bot]"):
        return None
    match = FINDING_MARKER_RE.match(str(first.get("body") or ""))
    return match.groupdict() if match and int(match.group("pr")) == pr else None


def superseded_threads(
    threads: Iterable[Mapping[str, Any]], *, pr: int, head: str, verdict: str
) -> list[str]:
    """Factory threads a new review replaces: every one on approval, older heads otherwise.

    The newest review sees the current code and re-raises whatever still applies,
    so an older factory thread must not hold the merge gate on its own.
    """
    found: list[str] = []
    for thread in threads:
        marker = thread_finding(thread, pr=pr)
        if marker and thread.get("id") and (verdict == APPROVE or marker["head"] != head):
            found.append(str(thread["id"]))
    return found


def left_lines(patch: str) -> list[range]:
    """Old-side line ranges each hunk replaces, for a compare from the finding's head."""
    hunks: list[range] = []
    for line in patch.splitlines():
        match = re.match(r"^@@ -(?P<start>\d+)(?:,(?P<count>\d+))? \+", line)
        if match:
            start = int(match.group("start"))
            hunks.append(range(start, start + max(int(match.group("count") or 1), 1)))
    return hunks


def fix_touched(
    compare_files: Iterable[Mapping[str, Any]], *, path: str, first: int, last: int
) -> bool:
    """Whether a compare from the finding's head changed any of its cited lines."""
    for item in compare_files:
        if item.get("filename") != path and item.get("previous_filename") != path:
            continue
        if item.get("status") == "removed":
            return True
        patch = item.get("patch")
        if isinstance(patch, str):
            return any(span.start <= last and first < span.stop for span in left_lines(patch))
    return False


def mission(head: str) -> str:
    """The reviewer's output contract. The prefix never starts a line, so an echo cannot parse."""
    return (
        "REVIEW OUTPUT CONTRACT: report every problem as a line-anchored finding. "
        "Do not edit code in this review. Print exactly one line that starts with "
        f"{PREFIX} followed by compact JSON: "
        f'{{"head":"{head}","findings":[{{"path":"src/app.py","line":42,"start_line":40,'
        '"severity":"blocking","body":"what is wrong","fix":"the change that fixes it"}]}. '
        "path is a file changed by this PR; line (and optional start_line) are line numbers in "
        "the head version of that file and must fall inside one diff hunk of this PR. "
        "severity is blocking (must change before merge) or non-blocking (advice). "
        "Use an empty findings list when nothing blocks. The factory decides the verdict from "
        "these findings alone: zero blocking findings approves, any blocking finding requests "
        "changes, and findings off the diff are dropped. A missing or malformed record fails "
        "this review run."
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    contract = commands.add_parser("mission", help="print the reviewer output contract")
    contract.add_argument("--head", required=True)
    check = commands.add_parser("check", help="validate a review log against a files listing")
    check.add_argument("--log", required=True)
    check.add_argument("--head", required=True)
    check.add_argument("--files", required=True, help="pulls/N/files JSON (a list or pages)")
    check.add_argument("--rejects", action="store_true")
    args = parser.parse_args(argv)
    if not HEAD_RE.fullmatch(args.head):
        parser.error("head must be a full lowercase SHA")
    if args.command == "mission":
        print(mission(args.head))
        return 0
    text = Path(args.log).read_text(encoding="utf-8", errors="replace")
    files = json.loads(Path(args.files).read_text(encoding="utf-8"))
    if files and isinstance(files[0], list):
        files = [row for page in files for row in page]
    record, error = parse_record(text, head=args.head)
    review = validate(record, error, files)
    print(json.dumps({"verdict": decide(review, rejects=args.rejects), **review.to_dict()}))
    return 0 if review.parsed else 3


if __name__ == "__main__":
    raise SystemExit(main())
