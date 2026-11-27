"""Issue-level acceptance evidence for exact-head factory approvals."""

import hashlib
import json
import re
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

# Callers load this module by file path, so its directory may not be on sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from factory_acceptance_criteria import (  # noqa: E402, F401
    BULLET,
    CONTEXT_HEADINGS,
    HEADING,
    HEADINGS,
    VERIFY_AFTER_MERGE,
    criteria,
    deferred,
    deferred_note,
    failure_summary,
    requirements,
    uncited,
)
from factory_adopter import MARKER_PREFIX  # noqa: E402

PREFIX = "FACTORY_ACCEPTANCE_V1="


def digest(body: str) -> str:
    """Return SHA-256 of the exact issue body.

    Args:
        body: Unmodified issue body.

    Returns:
        Hexadecimal SHA-256 digest.
    """
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def marker(issue: int, head: str, body: str) -> str:
    """Create a controller-only acceptance attestation.

    Args:
        issue: Linked issue number.
        head: Full reviewed PR head SHA.
        body: Exact issue revision.

    Returns:
        Standalone machine-readable acceptance marker.
    """
    return (
        f"<!-- {MARKER_PREFIX}-factory-acceptance-v1:"
        f"issue-{issue}:head-{head}:digest-{digest(body)} -->"
    )


def attested(controller_comments: Iterable[str], issue: int, head: str, body: str) -> bool:
    """Accept only an entire controller-bot comment matching the expected marker.

    Args:
        controller_comments: Complete comment bodies prefiltered to trusted controller bot.
        issue: Linked issue number.
        head: Exact reviewed head.
        body: Unmodified authoritative issue body.

    Returns:
        Whether the controller emitted a standalone attestation comment.
    """
    expected = marker(issue, head, body)
    return any(comment.strip() == expected for comment in controller_comments)


def report_from_text(review_text: str) -> Mapping[str, Any] | None:
    """Extract a unique structured acceptance report without truncation.

    Args:
        review_text: Complete worker review log.

    Returns:
        Parsed report, or None if ambiguous or invalid.
    """
    # OpenCode can emit assistant prose as JSONL text events instead of plain
    # lines. Only inspect the assistant's text parts: tool results and event
    # metadata can contain echoed examples or untrusted acceptance markers.
    lines: list[str] = []
    for raw_line in review_text.splitlines():
        if raw_line.startswith(PREFIX):
            lines.append(raw_line[len(PREFIX):])
            continue
        if not raw_line.startswith("{"):
            continue
        try:
            event = json.loads(raw_line)
        except ValueError:
            continue
        if not isinstance(event, dict) or event.get("type") != "text":
            continue
        part = event.get("part")
        if not isinstance(part, dict) or part.get("type") != "text":
            continue
        message = part.get("text")
        if not isinstance(message, str):
            continue
        lines.extend(line[len(PREFIX):] for line in message.splitlines()
                     if line.startswith(PREFIX))
    # Models often print their report and then cat or echo it again. Identical
    # copies are one report; two different reports stay ambiguous.
    reports: list[dict[str, Any]] = []
    for line in lines:
        while line.startswith(PREFIX):
            line = line[len(PREFIX):]
        if len(line) > 2000000:
            return None
        try:
            data = json.loads(line)
        except (TypeError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        if data not in reports:
            reports.append(data)
    return reports[0] if len(reports) == 1 else None


def validate(
    report: Mapping[str, Any] | None,
    issue: int,
    head: str,
    body: str,
    changed_paths: Iterable[str],
    *,
    non_closing: bool = False,
) -> list[str]:
    """Verify exact issue revision and evidence for each criterion.

    Args:
        report: Independent reviewer report, if present.
        issue: Canonical linked issue number.
        head: Reviewed PR head SHA.
        body: Fresh authoritative issue body.
        changed_paths: Paths in the current PR diff.
        non_closing: True for a review-only ``Part of #N`` handoff, where
            out-of-scope criteria may be reported as ``remaining``.

    Returns:
        Validation errors, empty only for a complete report.
    """
    if report is None:
        return ["missing or malformed FACTORY_ACCEPTANCE_V1 report"]
    if report.get("issue") != issue or report.get("head") != head:
        return ["report refers to a different issue or head"]
    if report.get("issue_digest") != digest(body):
        return ["issue requirements changed during review; require fresh review"]
    items = report.get("items")
    rubric = criteria(body)
    if not isinstance(items, list) or len(items) != len(rubric):
        return [f"report must cover all {len(rubric)} issue criteria"]
    changed = set(changed_paths)
    seen, errors = set(), []
    for item in items:
        if not isinstance(item, dict):
            errors.append("criterion must be a structured object")
            continue
        name = item.get("id")
        if (not isinstance(name, str) or
                name not in {f"A{i}" for i in range(1, len(rubric) + 1)} or
                name in seen):
            errors.append(f"invalid/duplicate criterion {name!r}")
            continue
        seen.add(name)
        if rubric[int(name[1:]) - 1].verify_after_merge:
            continue
        status = item.get("status")
        allowed = {"pass", "remaining"} if non_closing else {"pass"}
        if status not in allowed:
            errors.append(f"{name}: missing PASS evidence")
            continue
        if status == "remaining":
            evidence = item.get("evidence")
            if not isinstance(evidence, str) or len(evidence.strip()) < 30:
                errors.append(f"{name}: name the remaining work with evidence")
            continue
        missing = uncited(item.get("file"), changed)
        if missing is None:
            errors.append(f"{name}: cite a file changed by this PR (no file cited)")
        elif missing:
            errors.append(f"{name}: cite a file changed by this PR (not changed: {', '.join(missing)})")
        evidence = item.get("evidence")
        if not isinstance(evidence, str) or len(evidence.strip()) < 30:
            errors.append(f"{name}: give specific implementation evidence")
        test = item.get("test")
        if not isinstance(test, str) or len(test.strip()) < 12 or test.lower().strip() in {
            "not tested", "tests passed", "no tests", "not needed"
        }:
            errors.append(f"{name}: name the test/check and what it verifies")
    if len(seen) != len(rubric):
        errors.append("not every criterion was evaluated")
    return errors


def rubric(body: str) -> str:
    """Number every extracted requirement for the reviewer.

    Args:
        body: Complete GitHub issue body.

    Returns:
        Numbered rubric with contextual source headings.
    """
    return "\n".join(f"A{i}: {item}" for i, item in enumerate(requirements(body), 1))



CLOSING_RE = re.compile(
    r"(?i)\b(?:closes?|closed|fix(?:es|ed)?|resolves?|resolved)\s+#(\d+)"
)
BRANCH_RE = re.compile(r"^factory/\d+-(\d+)-")
# The canonical non-closing handoff line names the issue a PR advances without
# claiming closure. ``\r?`` keeps CRLF PR bodies matching.
PART_OF_RE = re.compile(
    r"(?im)^[ \t]*part[ \t]+of[ \t]+#(?P<issue>\d+)"
    r"(?:[ \t]+and[ \t]+governed[ \t]+by[ \t]+#\d+)?\.[ \t]*\r?$"
)


def linked_issue_mode(branch: str, body: str) -> tuple[int | None, bool]:
    """Resolve a linked issue and whether the link is review-only.

    Args:
        branch: PR branch name.
        body: PR description.

    Returns:
        ``(issue, non_closing)``. A canonical branch fixes the issue
        identity, while a matching explicit ``Part of #N.`` line can make
        the PR non-closing. A closing keyword always takes precedence.
    """
    match = BRANCH_RE.match(branch)
    if match:
        issue = int(match.group(1))
        # Branch identity owns the issue, but a matching explicit "Part of"
        # identifies a non-closing delivery slice. Never let an unrelated
        # Part of reference change ownership or relax acceptance.
        part = PART_OF_RE.search(body)
        non_closing = bool(
            part
            and int(part.group("issue")) == issue
            and CLOSING_RE.search(body) is None
        )
        return issue, non_closing
    match = CLOSING_RE.search(body)
    if match:
        return int(match.group(1)), False
    match = PART_OF_RE.search(body)
    return (int(match.group("issue")), True) if match else (None, False)


def linked_issue(branch: str, body: str) -> int | None:
    """Resolve a linked issue using the same semantics as the controller.

    Args:
        branch: PR branch name.
        body: PR description.

    Returns:
        Canonical linked issue number, if a supported link was found.
    """
    return linked_issue_mode(branch, body)[0]


OWNER_EXEMPT_MISSION = (
    "\nISSUE ACCEPTANCE NOT REQUIRED: the repository owner wrote this PR and it links no issue. "
    "Review the diff against the PR description; your line-anchored findings decide the verdict.\n"
)


def owner_exempt(pr: Mapping[str, Any], repo: str) -> bool:
    """An owner PR with no linked issue merges on its line review (owner decision 2026-11-27)."""
    author = pr.get("author")
    login = str(author.get("login") or "") if isinstance(author, Mapping) else ""
    branch, body = str(pr.get("headRefName") or ""), str(pr.get("body") or "")
    is_owner = bool(login) and login.lower() == repo.split("/")[0].lower()
    return is_owner and linked_issue(branch, body) is None


def mission(issue: int, head: str, body: str, non_closing: bool = False) -> str:
    """Give the reviewer an exact, complete acceptance schema for this issue."""
    revision = digest(body)
    rubric = criteria(body)
    count = len(rubric)
    if non_closing:
        scope = (
            " This is a review-only `Part of` handoff: it does not close the issue."
            " Criteria this PR delivers must be `pass`; criteria outside its declared"
            " scope must be `remaining` with concrete evidence naming the outstanding"
            " work. Never mark in-scope work `remaining` to bypass verification."
        )
    else:
        scope = " Do not approve when any requirement is missing or unverified."
    # Build every ID rather than demonstrating A1 alone: in the production
    # incident the model copied the old one-item example despite a longer rubric.
    # Placeholders are deliberately invalid, not synthetic acceptance evidence.
    items = ",".join(
        f'{{"id":"A{index}","status":"{"deferred" if item.verify_after_merge else "unverified"}",'
        '"file":"","evidence":"","test":""}'
        for index, item in enumerate(rubric, 1)
    )
    later = "".join(f" A{index}" for index, item in enumerate(rubric, 1) if item.verify_after_merge)
    if later:
        scope += (
            f" Criteria marked [{VERIFY_AFTER_MERGE}] ({later.strip()}) need a deployed"
            " environment: report them with status `deferred`. They do not block"
            " approval and keep the issue open after merge."
        )
    template = (
        f'{PREFIX}{{"issue":{issue},"head":"{head}",'
        f'"issue_digest":"{revision}","items":[{items}]}}'
    )
    return (
        f"\nISSUE ACCEPTANCE: #{issue}, head {head}, issue digest {revision}.\n"
        "Independently review EVERY numbered requirement against the exact diff"
        " and observed tests." + scope + "\n"
        f"ACCEPTANCE_CRITERIA_COUNT={count}\n"
        "OUTPUT CONTRACT: Immediately before FACTORY_GATE_READY, write exactly"
        " one SINGLE line beginning with the literal FACTORY_ACCEPTANCE_V1= at"
        " column zero, followed without a space by one JSON object. Do not use"
        " a bare JSON object, Markdown fence, summary, or shortened A1 example."
        f" It must contain exactly A1 through A{count} in order, with no omissions.\n"
        "The following is a SHAPE ONLY, not approval: replace EVERY unverified"
        " placeholder with your independently corroborated status, a real changed"
        " file, implementation evidence and the actual test/check observed."
        " Never copy placeholder data or invent test results. If even one"
        " criterion is missing, unsafe or unsupported, finish with"
        " FACTORY_GATE_BLOCKED rather than READY.\n"
        f"{template}\n"
        + "\n".join(
            f"A{index}{f' [{VERIFY_AFTER_MERGE}]' if item.verify_after_merge else ''}: {item.text}"
            for index, item in enumerate(rubric, 1)
        )
    )


MISSION_IDENTITY_RE = re.compile(
    r"(?m)^ISSUE ACCEPTANCE: #([1-9][0-9]*), head ([a-f0-9]{40}), issue digest ([a-f0-9]{64})\.$"
)
PASS_SYNONYMS = frozenset({
    "pass", "passed", "passes", "verified", "met", "satisfied", "ok", "done",
    "complete", "completed", "yes", "true", "fixed", "implemented", "addressed",
    "resolved",
})


def normalize_report(report: Any, mission_text: str) -> Any:
    """Fill identity from the trusted mission and map pass synonyms.

    Models copy the 64-hex digest and SHA unreliably and write "verified" for
    "pass". The mission identity is what the worker reviewed; the controller
    still compares the digest with the live issue, so an edited issue fails.
    """
    identity = MISSION_IDENTITY_RE.findall(mission_text)
    if not isinstance(report, dict) or len(identity) != 1:
        return report
    issue, head, revision = identity[0]
    fixed = {**report, "issue": int(issue), "head": head, "issue_digest": revision}
    items = report.get("items")
    if isinstance(items, list):
        fixed["items"] = [
            {**item, "status": "pass"}
            if isinstance(item, dict) and str(item.get("status", "")).strip().lower() in PASS_SYNONYMS
            else item
            for item in items
        ]
    return fixed


def replace_report_line(text: str, report: Mapping[str, Any]) -> str:
    """Rewrite the single acceptance line so the controller sees the normalized report."""
    line = PREFIX + json.dumps(report, separators=(",", ":"))
    return "\n".join(line if row.startswith(PREFIX) else row for row in text.split("\n"))


def report_matches_mission(report: Mapping[str, Any] | None, mission_text: str) -> bool:
    """Shape-check a model report against the trusted exact-head issue mission.

    This is a preliminary fail-closed check, never an acceptance attestation.
    The review controller still re-fetches the current issue and independently
    verifies evidence, changed paths, checks, and reviewer provenance.
    """
    if report is None:
        return False
    identity = re.findall(
        r"(?m)^ISSUE ACCEPTANCE: #([1-9][0-9]*), head ([a-f0-9]{40}), issue digest ([a-f0-9]{64})\.$",
        mission_text,
    )
    counts = re.findall(
        r"(?m)^ACCEPTANCE_CRITERIA_COUNT=([1-9][0-9]*)$", mission_text
    )
    if len(identity) != 1 or len(counts) != 1:
        return False
    issue, head, revision = identity[0]
    count = int(counts[0])
    if count > 1000 or count < 1:
        return False
    if (report.get("issue"), report.get("head"), report.get("issue_digest")) != (
        int(issue), head, revision
    ):
        return False
    items = report.get("items")
    return (
        isinstance(items, list)
        and len(items) == count
        and all(
            isinstance(item, dict)
            and item.get("id") == f"A{index}"
            and item.get("status") in {"pass", "remaining", "deferred"}
            for index, item in enumerate(items, 1)
        )
    )

if __name__ == "__main__":
    with __import__("contextlib").suppress(ImportError):
        __import__("factory_crash").install()
    try:
        if sys.argv[1:] == ["rubric"]:
            print(rubric(sys.stdin.read()))
        elif sys.argv[1:] == ["resolve"]:
            data = json.load(sys.stdin)
            number = linked_issue(
                str(data.get("headRefName") or ""),
                str(data.get("body") or ""),
            )
            if number is None:
                raise ValueError("PR has no supported linked issue")
            print(number)
        elif sys.argv[1:] == ["no-issue-mission"]:
            from factory_adopter import repository

            if not owner_exempt(json.load(sys.stdin), repository()):
                raise ValueError("only an owner-authored PR may omit a linked issue")
            print(OWNER_EXEMPT_MISSION, end="")
        elif sys.argv[1:] == ["non-closing"]:
            data = json.load(sys.stdin)
            print(
                "1"
                if linked_issue_mode(
                    str(data.get("headRefName") or ""),
                    str(data.get("body") or ""),
                )[1]
                else "0"
            )
        elif len(sys.argv) in {4, 5} and sys.argv[1] == "mission":
            data = json.load(sys.stdin)
            if not isinstance(data.get("body"), str):
                raise ValueError("issue body missing")
            print(
                mission(
                    int(sys.argv[2]),
                    sys.argv[3],
                    data["body"],
                    non_closing=len(sys.argv) == 5 and sys.argv[4] == "1",
                )
            )
        else:
            raise ValueError("usage: rubric | resolve | mission ISSUE HEAD")
    except (ValueError, TypeError, KeyError) as exc:
        print(f"acceptance input unavailable: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
