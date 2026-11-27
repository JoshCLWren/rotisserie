"""A person's reopen means the merged PR did not finish the issue (#212)."""

from __future__ import annotations

import json
import unittest
from unittest import mock

import factory_post_merge_closure_github as github
from factory_post_merge_closure import close_merged_pr_issue


def _reopen(typename: str, at: str = "2026-11-23T19:53:00Z") -> str:
    node = {"createdAt": at, "actor": {"__typename": typename, "login": "someone"}}
    issue = {"timelineItems": {"nodes": [node]}}
    return json.dumps({"data": {"repository": {"issue": issue}}})


class PersonReopenTests(unittest.TestCase):
    def _close(self, reopened: bool | str) -> tuple[str, mock.MagicMock]:
        info = {"branch": "", "head": "h", "merge": "m", "body": "Closes #5695", "state": "MERGED"}
        issue = {"state": "OPEN", "body": "", "labels": ["factory", "factory:unowned"]}
        with (
            mock.patch.object(github, "_pr_info", return_value=info),
            mock.patch.object(github, "_issue_view", return_value=issue),
            mock.patch.object(github, "_reopened_by_person", return_value=reopened),
            mock.patch.object(github, "_close_issue") as close,
        ):
            return close_merged_pr_issue(5696), close

    def test_a_person_reopen_keeps_the_issue_open(self) -> None:
        result, close = self._close(True)
        self.assertEqual(result, "reopened-by-person")
        close.assert_not_called()

    def test_an_unreadable_reopen_history_never_closes(self) -> None:
        result, close = self._close("unknown")
        self.assertEqual(result, "reopen-lookup-failed")
        close.assert_not_called()

    def test_only_a_user_reopen_counts_as_a_person(self) -> None:
        with mock.patch.object(github, "_repo_slug", return_value="acme/widgets"):
            with mock.patch.object(github, "_run_gh", return_value=_reopen("User")):
                self.assertIs(github._reopened_by_person(5695), True)
            with mock.patch.object(github, "_run_gh", return_value=_reopen("Bot")):
                self.assertIs(github._reopened_by_person(5695), False)
            with mock.patch.object(github, "_run_gh", side_effect=RuntimeError("gh failed")):
                self.assertEqual(github._reopened_by_person(5695), "unknown")


    def test_a_reopen_before_the_pr_was_opened_does_not_hold_its_closure(self) -> None:
        """ComicPile #5525 was reopened Nov 23; the PR that closes it opened Nov 26."""
        with mock.patch.object(github, "_repo_slug", return_value="acme/widgets"):
            with mock.patch.object(github, "_run_gh", return_value=_reopen("User")):
                self.assertIs(github._reopened_by_person(5525, "2026-11-26T02:20:00Z"), False)
                self.assertIs(github._reopened_by_person(5525, "2026-11-22T00:00:00Z"), True)


if __name__ == "__main__":
    unittest.main()
