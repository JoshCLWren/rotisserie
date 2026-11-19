#!/usr/bin/env python3
"""Pure decision helper for fixed-model PR repair safety."""

from __future__ import annotations

import argparse
import json
import unittest
from typing import cast

FACTORY_CONTROL_PREFIXES: tuple[str, ...] = (
    ".github/workflows/fixed-model-",
    ".github/workflows/free-model-factory-",
    ".github/scripts/fixed-model-",
    ".github/scripts/free-model-factory-",
    ".github/scripts/classify-fixed-model-run.py",
    ".github/scripts/validate-free-model-factories.py",
    ".github/scripts/factory_roster.py",
    ".github/scripts/factory_model_catalog.py",
    ".github/scripts/factory_model_retirement.py",
    ".github/workflows/factory-model-discovery.yml",
    ".github/factory-expected-workers.json",
    ".github/free-model-factories.tsv",
)

FACTORY_TASK_NEEDLES: tuple[str, ...] = (
    "fixed-model",
    "free-model-factory",
    "factory fleet",
    "factory worker",
    "factory lease",
    "factory contamination",
    "factory guard",
    "factory dispatcher",
    "factory runner",
    "opencode-free-model-factory",
)


def is_factory_control_path(path: str) -> bool:
    """Return True when a path is fixed-model factory control infrastructure."""
    normalized = path.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return any(
        normalized == prefix.rstrip("/") or normalized.startswith(prefix)
        for prefix in FACTORY_CONTROL_PREFIXES
    )


def task_allows_factory_control(title: str, body: str) -> bool:
    """Return True when the selected task explicitly concerns factory infrastructure."""
    text = f"{title}\n{body}".lower()
    return any(needle in text for needle in FACTORY_TASK_NEEDLES)


def unrelated_repair(previous_pr_files: set[str], latest_commit_files: set[str]) -> bool:
    """Reject only an existing-PR repair whose latest commit has zero overlap.

    Empty previous PR diffs represent first implementation commits and are not
    treated as adopted-PR repair work.

    Args:
        previous_pr_files: Paths already present on the PR before the latest commit.
        latest_commit_files: Paths touched by the latest commit.

    Returns:
        True when the latest commit is an unrelated repair that should be rejected.

    """
    return bool(
        previous_pr_files
        and latest_commit_files
        and previous_pr_files.isdisjoint(latest_commit_files)
    )


def reject_decision(
    previous_pr_files: set[str],
    latest_commit_files: set[str],
    *,
    title: str = "",
    body: str = "",
    merge_in_progress: bool = False,
    cherry_pick_in_progress: bool = False,
    revert_in_progress: bool = False,
    unmerged_entries: bool = False,
    conflict_markers: bool = False,
    head_changed: bool = False,
    foreign_main_commits: bool = False,
) -> dict[str, object]:
    """Decide whether a factory persistence attempt must be rejected.

    Rejection is fail-closed and deterministic. Checks run in order:
    1. Unclean git state is always rejected so the wrapper can never persist
       a model-created merge, rebase, or conflict resolution. This covers an
       in-progress merge/cherry-pick/revert, unmerged index entries, literal
       conflict-marker text in changed files, a HEAD that moved away from the
       checked-out target (a model-committed merge or rebase of main), and
       commits from main folded into the branch.
    2. Factory-control paths already present in the prior PR diff are rejected
       for non-factory tasks, so an adopted PR branch carrying stale factory
       contamination cannot be pushed further.
    3. Zero overlap with an existing PR diff is rejected.
    4. Factory-control paths in the latest diff are rejected unless the
       selected task is about fixed-model factory infrastructure.

    Args:
        previous_pr_files: Paths already on the PR before local changes.
        latest_commit_files: Paths in the staged/uncommitted factory diff.
        title: Selected issue or PR title used for scope checks.
        body: Selected issue or PR body used for scope checks.
        merge_in_progress: A git merge is under way (MERGE_HEAD exists).
        cherry_pick_in_progress: A cherry-pick is under way.
        revert_in_progress: A revert is under way.
        unmerged_entries: The index has unmerged (conflicted) entries.
        conflict_markers: Changed files contain literal git conflict markers.
        head_changed: HEAD no longer equals the checked-out target head.
        foreign_main_commits: The branch now contains commits from main that
            were not reachable at checkout time.

    Returns:
        JSON-serializable decision with reject flag and reason.

    """
    git_state = {
        "merge_in_progress": merge_in_progress,
        "cherry_pick_in_progress": cherry_pick_in_progress,
        "revert_in_progress": revert_in_progress,
        "unmerged_entries": unmerged_entries,
        "conflict_markers": conflict_markers,
        "head_changed": head_changed,
        "foreign_main_commits": foreign_main_commits,
    }
    if any(git_state.values()):
        return {
            "reject": True,
            "reason": "unclean-git-state",
            "git_state": git_state,
            "factory_control_files": [],
        }

    latest = set()
    for path in latest_commit_files:
        if not path:
            continue
        normalized = path.replace("\\", "/")
        while normalized.startswith("./"):
            normalized = normalized[2:]
        latest.add(normalized)
    previous = set()
    for path in previous_pr_files:
        if not path:
            continue
        normalized = path.replace("\\", "/")
        while normalized.startswith("./"):
            normalized = normalized[2:]
        previous.add(normalized)

    prior_control_files = sorted(path for path in previous if is_factory_control_path(path))
    if prior_control_files and not task_allows_factory_control(title, body):
        return {
            "reject": True,
            "reason": "factory-control-in-prior-pr-diff",
            "factory_control_files": prior_control_files,
        }

    if unrelated_repair(previous, latest):
        return {
            "reject": True,
            "reason": "zero-overlap-with-existing-pr-diff",
            "factory_control_files": sorted(path for path in latest if is_factory_control_path(path)),
        }

    control_files = sorted(path for path in latest if is_factory_control_path(path))
    if control_files and not task_allows_factory_control(title, body):
        return {
            "reject": True,
            "reason": "factory-control-out-of-scope",
            "factory_control_files": control_files,
        }

    return {
        "reject": False,
        "reason": "allowed",
        "factory_control_files": control_files,
    }


class GuardTests(unittest.TestCase):
    """Unit tests for unrelated-repair and factory-control detection."""

    def test_unrelated_repair_is_rejected(self) -> None:
        """Zero-overlap repair on an existing PR is rejected."""
        decision = reject_decision(
            {".github/scripts/factory-visibility.cjs"},
            {"app/schemas/release.py"},
            title="After rating two d4s appear?",
            body="Duplicate dice on the roll screen.",
        )
        self.assertTrue(decision["reject"])
        self.assertEqual(decision["reason"], "zero-overlap-with-existing-pr-diff")

    def test_repair_with_overlap_is_allowed_even_with_new_tests(self) -> None:
        """Overlap with prior PR files allows the repair, including new tests."""
        decision = reject_decision(
            {"app/service.py"},
            {"app/service.py", "tests/test_service.py"},
            title="Fix service bug",
            body="Product bug",
        )
        self.assertFalse(decision["reject"])

    def test_first_implementation_commit_is_not_rejected(self) -> None:
        """Empty previous PR set means first implementation, not a repair."""
        decision = reject_decision(
            set(),
            {"app/new_feature.py"},
            title="Add feature",
            body="Product feature",
        )
        self.assertFalse(decision["reject"])

    def test_empty_latest_commit_is_not_rejected(self) -> None:
        """Empty latest commit is not treated as an unrelated repair."""
        decision = reject_decision(
            {"app/service.py"},
            set(),
            title="Fix service bug",
            body="Product bug",
        )
        self.assertFalse(decision["reject"])

    def test_mixed_factory_control_on_product_pr_is_rejected(self) -> None:
        """Factory control edits are rejected even when mixed with product overlap."""
        decision = reject_decision(
            {
                "frontend/src/components/Dice3D.tsx",
                "frontend/src/test/issue-1182-header-die-size.spec.ts",
            },
            {
                "frontend/src/test/issue-1182-header-die-size.spec.ts",
                ".github/scripts/classify-fixed-model-run.py",
                ".github/scripts/fixed-model-guard.py",
            },
            title="After raiting two d4s appear?",
            body="They're both rotating on the dice selection screen.",
        )
        self.assertTrue(decision["reject"])
        self.assertEqual(decision["reason"], "factory-control-out-of-scope")
        self.assertIn(".github/scripts/classify-fixed-model-run.py", decision["factory_control_files"])

    def test_factory_infra_task_may_edit_factory_control(self) -> None:
        """Tasks about the fixed-model fleet may touch factory control paths."""
        decision = reject_decision(
            {".github/scripts/fixed-model-guard.py"},
            {".github/scripts/fixed-model-guard.py", ".github/free-model-factories.tsv"},
            title="Guard fixed-model PR repairs and runner leases",
            body="Prevent fixed-model factory contamination across the factory fleet.",
        )
        self.assertFalse(decision["reject"])

    def test_merge_in_progress_is_rejected(self) -> None:
        """An in-progress git merge (MERGE_HEAD) fails closed before any push."""
        decision = reject_decision(
            {"frontend/src/pages/RollPage/components/RatingView.tsx"},
            {"frontend/src/pages/RollPage/components/RatingView.tsx"},
            title="Continuity correction from the Roll screen",
            body="Product feature",
            merge_in_progress=True,
        )
        self.assertTrue(decision["reject"])
        self.assertEqual(decision["reason"], "unclean-git-state")
        self.assertTrue(cast(dict, decision["git_state"])["merge_in_progress"])

    def test_conflict_markers_are_rejected(self) -> None:
        """Literal git conflict-marker text in the diff fails closed."""
        decision = reject_decision(
            {"app/schemas/release.py"},
            {"app/schemas/release.py"},
            title="Release writer regression coverage",
            body="Product work",
            conflict_markers=True,
        )
        self.assertTrue(decision["reject"])
        self.assertEqual(decision["reason"], "unclean-git-state")
        self.assertTrue(cast(dict, decision["git_state"])["conflict_markers"])

    def test_cherry_pick_and_revert_in_progress_are_rejected(self) -> None:
        """Cherry-pick and revert heads fail closed exactly like MERGE_HEAD."""
        for flag in ("cherry_pick_in_progress", "revert_in_progress"):
            with self.subTest(flag=flag):
                decision = reject_decision(
                    {"app/service.py"},
                    {"app/service.py"},
                    title="Fix service bug",
                    body="Product bug",
                    **{flag: True},
                )
                self.assertTrue(decision["reject"])
                self.assertEqual(decision["reason"], "unclean-git-state")

    def test_unmerged_index_entries_are_rejected(self) -> None:
        """Unmerged (conflicted) index entries fail closed."""
        decision = reject_decision(
            {"app/service.py"},
            {"app/service.py"},
            title="Fix service bug",
            body="Product bug",
            unmerged_entries=True,
        )
        self.assertTrue(decision["reject"])
        self.assertEqual(decision["reason"], "unclean-git-state")

    def test_head_changed_after_model_merge_is_rejected(self) -> None:
        """A HEAD that moved from the checkout target blocks model merge/rebase."""
        decision = reject_decision(
            {"frontend/src/pages/RollPage/components/RatingView.tsx"},
            {"frontend/src/pages/RollPage/components/RatingView.tsx"},
            title="Continuity correction from the Roll screen",
            body="Product feature",
            head_changed=True,
        )
        self.assertTrue(decision["reject"])
        self.assertEqual(decision["reason"], "unclean-git-state")
        self.assertTrue(cast(dict, decision["git_state"])["head_changed"])

    def test_foreign_main_commits_are_rejected(self) -> None:
        """Main commits folded into the branch (rebase/merge) fail closed."""
        decision = reject_decision(
            {"frontend/src/pages/QueuePage.tsx"},
            {"frontend/src/pages/QueuePage.tsx"},
            title="Decompose QueuePage",
            body="Product refactor",
            foreign_main_commits=True,
        )
        self.assertTrue(decision["reject"])
        self.assertEqual(decision["reason"], "unclean-git-state")

    def test_stale_branch_guard_helper_cannot_weaken_trusted_guard(self) -> None:
        """A stale branch guard helper cannot weaken the trusted guard.

        The old branch helper only implemented zero-overlap detection; the
        trusted guard copy still rejects factory control paths regardless of
        the stale helper's presence in the prior or latest diff sets.
        """
        decision = reject_decision(
            {
                ".github/scripts/fixed-model-guard.py",
                "frontend/src/components/ContinuityCorrectionDialog.tsx",
                "frontend/src/pages/RollPage/components/RatingView.tsx",
            },
            {
                "frontend/src/unit/ContinuityCorrectionDialog.test.tsx",
                ".github/scripts/classify-fixed-model-run.py",
                ".github/scripts/fixed-model-guard.py",
            },
            title="Add in-context continuity correction from the Roll rating screen",
            body="Part of #852. Reader can open the ContinuityCorrectionDialog and add the current issue.",
        )
        self.assertTrue(decision["reject"])
        self.assertIn(decision["reason"], ("factory-control-in-prior-pr-diff", "factory-control-out-of-scope"))
        self.assertIn(".github/scripts/fixed-model-guard.py", decision["factory_control_files"])

    def test_prior_pr_diff_factory_control_is_rejected_on_product_task(self) -> None:
        """Factory control already in the PR diff blocks further pushes."""
        decision = reject_decision(
            {
                ".github/scripts/classify-fixed-model-run.py",
                ".github/scripts/fixed-model-guard.py",
                "frontend/src/components/ContinuityCorrectionDialog.tsx",
            },
            {
                "frontend/src/components/ContinuityCorrectionDialog.tsx",
                "frontend/src/unit/ContinuityCorrectionDialog.test.tsx",
            },
            title="Add in-context continuity correction from the Roll rating screen",
            body="Part of #852.",
        )
        self.assertTrue(decision["reject"])
        self.assertEqual(decision["reason"], "factory-control-in-prior-pr-diff")
        self.assertIn(".github/scripts/fixed-model-guard.py", decision["factory_control_files"])

    def test_factory_task_allows_prior_factory_control_diff(self) -> None:
        """Factory infrastructure tasks may carry prior factory-control diffs."""
        decision = reject_decision(
            {".github/scripts/fixed-model-guard.py"},
            {".github/scripts/fixed-model-guard.py", ".github/free-model-factories.tsv"},
            title="Guard fixed-model PR repairs and runner leases",
            body="Prevent fixed-model factory contamination across the factory fleet.",
        )
        self.assertFalse(decision["reject"])

    def test_legacy_unrelated_repair_helper_still_matches(self) -> None:
        """Preserve the original zero-overlap helper behavior."""
        self.assertTrue(
            unrelated_repair({".github/scripts/factory-visibility.cjs"}, {"app/schemas/release.py"})
        )
        self.assertFalse(unrelated_repair({"app/service.py"}, {"app/service.py", "tests/test_service.py"}))


def main() -> int:
    """CLI entrypoint for the repair guard decision or self-tests.

    Returns:
        Process exit code (0 on success).

    """
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--previous-json")
    parser.add_argument("--latest-json")
    parser.add_argument("--git-state", default="{}")
    parser.add_argument("--title", default="")
    parser.add_argument("--body", default="")
    args = parser.parse_args()

    if args.self_test:
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(GuardTests)
        return 0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1

    previous = set(json.loads(args.previous_json or "[]"))
    latest = set(json.loads(args.latest_json or "[]"))
    git_state = json.loads(args.git_state or "{}")
    print(
        json.dumps(
            reject_decision(
                previous,
                latest,
                title=args.title,
                body=args.body,
                merge_in_progress=git_state.get("merge_in_progress", False),
                cherry_pick_in_progress=git_state.get("cherry_pick_in_progress", False),
                revert_in_progress=git_state.get("revert_in_progress", False),
                unmerged_entries=git_state.get("unmerged_entries", False),
                conflict_markers=git_state.get("conflict_markers", False),
                head_changed=git_state.get("head_changed", False),
                foreign_main_commits=git_state.get("foreign_main_commits", False),
            )
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
