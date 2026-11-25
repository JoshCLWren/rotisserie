"""Which merged PRs close their issue, with ComicPile's Nov 24 merges as fixtures."""

from __future__ import annotations

import contextlib
import unittest
from collections.abc import Iterator
from unittest import mock

import factory_implementation_delivery as implementation_delivery
import factory_post_merge_closure_github as github
from factory_merge_linkage import (
    CLOSING_KEYWORD,
    FACTORY_BRANCH,
    NO_LINK,
    NON_CLOSING,
    TITLE_ONLY,
    explain,
    linkage,
)
from factory_post_merge_closure import decide

READ_NOW_BODY = (
    "Part of #5751.\n\nManual Read now opened a compact card.\n\n## Not done here\n\n"
    "- The issue needs real authenticated acceptance before it can close.\n"
)
CREDENTIAL_BODY = (
    "Part of #5695.\n\nDeliberately **not** a closing PR: the authenticated production "
    "acceptance in the continuation note is still required.\n"
)
STALE_ISSUE_LABELS = [
    "bug",
    "factory",
    "factory:unowned",
    "factory:changes-requested",
    "ralph-status:in-review",
]


@contextlib.contextmanager
def merged_pr(
    *, branch: str, body: str, title: str, reopened: bool = False
) -> Iterator[dict[str, mock.MagicMock]]:
    info = {
        "branch": branch,
        "head": "a" * 40,
        "merge": "b" * 40,
        "body": body,
        "state": "MERGED",
        "title": title,
    }
    issue = {"state": "OPEN", "body": "Fix the thing.", "labels": list(STALE_ISSUE_LABELS)}
    with (
        mock.patch.object(github, "_pr_info", return_value=info),
        mock.patch.object(github, "_issue_view", return_value=issue),
        mock.patch.object(github, "_reopened_by_person", return_value=reopened),
        mock.patch.object(github, "_has_dependency_block_marker", return_value=False),
        mock.patch.object(github, "_issue_has_open_successor_pr", return_value=False),
        mock.patch.object(github, "_declared_dependency_blockers", return_value=([], True)),
        mock.patch.object(github, "_has_closure_marker", return_value=False),
        mock.patch.object(
            implementation_delivery, "complete_pr_diff", return_value=(["app/x.py"], "+x = 1\n")
        ),
        mock.patch.object(
            implementation_delivery, "pr_delivers_implementation", return_value=(True, "ok")
        ),
        mock.patch.object(github, "_replace_labels") as relabel,
        mock.patch.object(github, "_comment_issue") as comment,
        mock.patch.object(github, "_close_issue") as close,
    ):
        yield {"relabel": relabel, "comment": comment, "close": close}


class MergeLinkageTests(unittest.TestCase):
    def test_a_part_of_slice_on_a_one_number_factory_branch_does_not_close(self) -> None:
        link = linkage(
            "factory/5751-read-now-roll-parity",
            READ_NOW_BODY,
            "Read now opens the same comic details a roll shows (#5751)",
        )
        self.assertEqual((link.kind, link.issue, link.named), (NON_CLOSING, None, 5751))

    def test_a_declared_non_closing_owner_merge_does_not_close(self) -> None:
        link = linkage(
            "freebuff/aad5cd343387904c5c4f8a4c",
            CREDENTIAL_BODY,
            "Normalize COMICVINE_API_KEY at one credential boundary (#5695)",
        )
        self.assertEqual((link.kind, link.issue, link.named), (NON_CLOSING, None, 5695))

    def test_a_part_of_line_wins_over_the_canonical_branch(self) -> None:
        link = linkage("factory/86-4719-kilo-auto", "Part of #4719.", "")
        self.assertEqual((link.kind, link.issue), (NON_CLOSING, None))

    def test_a_title_only_reference_is_not_a_factory_record(self) -> None:
        link = linkage("freebuff/abc", "Normalizes the key.", "Normalize the key (#5695)")
        self.assertEqual((link.kind, link.issue, link.named), (TITLE_ONLY, None, 5695))

    def test_closing_keyword_and_canonical_branch_close(self) -> None:
        self.assertEqual(linkage("freebuff/abc", "Closes #5695.").kind, CLOSING_KEYWORD)
        self.assertEqual(linkage("factory/12-5751-kilo", "").kind, FACTORY_BRANCH)
        self.assertEqual(linkage("factory/12-5751-kilo", "").issue, 5751)
        self.assertEqual(linkage("main-fix", "Refactor.").kind, NO_LINK)

    def test_the_explanation_names_the_issue_and_the_reason(self) -> None:
        line = explain(5756, "no-linked-issue", linkage("factory/5751-x", READ_NOW_BODY))
        self.assertEqual(
            line,
            "Post-merge closure: PR #5756 -> issue #5751: no-linked-issue "
            "(the PR says it is only part of #5751, so #5751 stays open)",
        )


class MergeClosureDecisionTests(unittest.TestCase):
    def test_a_drain_merged_factory_branch_pr_closes_and_clears_work_labels(self) -> None:
        with merged_pr(branch="factory/12-5751-kilo-auto", body="", title="Fix") as calls:
            outcome, line = decide(5756)
        self.assertEqual(outcome, "closed")
        self.assertIn("-> issue #5751: closed (the factory branch names #5751)", line)
        calls["close"].assert_called_once_with(5751)
        labels = calls["relabel"].call_args.args[1]
        self.assertEqual(labels, ["bug", "factory", "ralph-status:done"])

    def test_an_owner_merged_pr_with_a_closing_keyword_closes(self) -> None:
        with merged_pr(branch="freebuff/aad5", body="Closes #5695.", title="Fix") as calls:
            outcome, _line = decide(5766)
        self.assertEqual(outcome, "closed")
        calls["close"].assert_called_once_with(5695)

    def test_a_person_reopened_issue_stays_open(self) -> None:
        with merged_pr(
            branch="factory/12-5751-kilo", body="Closes #5751.", title="Fix", reopened=True
        ) as calls:
            outcome, _line = decide(5756)
        self.assertEqual(outcome, "reopened-by-person")
        calls["close"].assert_not_called()

    def test_title_only_and_part_of_merges_leave_the_issue_untouched(self) -> None:
        cases = [
            ("freebuff/abc", "Normalizes the key.", "Normalize the key (#5695)", "only the title"),
            ("freebuff/aad5", CREDENTIAL_BODY, "Normalize (#5695)", "only part of #5695"),
            ("factory/5751-read-now", READ_NOW_BODY, "Read now (#5751)", "only part of #5751"),
        ]
        for branch, body, title, reason in cases:
            with (
                self.subTest(branch=branch),
                merged_pr(branch=branch, body=body, title=title) as calls,
            ):
                outcome, line = decide(1)
                self.assertEqual(outcome, "no-linked-issue")
                self.assertIn(reason, line)
                calls["close"].assert_not_called()
                calls["relabel"].assert_not_called()


if __name__ == "__main__":
    unittest.main()
