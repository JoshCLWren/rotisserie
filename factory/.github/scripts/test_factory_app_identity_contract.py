#!/usr/bin/env python3
"""Durable factory GitHub App identity contract tests.

This test file asserts the contract that #3142 is one instance of.
It does not duplicate #3142 tests; it asserts the cross-cutting rules
that every durable worker App must satisfy.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
REPO = SCRIPTS.parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from factory_review_policy import (  # noqa: E402
    TRUSTED_MARKER_APP_SLUGS,
    approval_can_promote,
    head_has_authorized_approval,
    performed_via_untrusted_app,
    trusted_comment_bodies,
)
from factory_work_policy import (  # noqa: E402
    TRUSTED_FACTORY_APP_SLUGS,
    comment_is_trusted,
)
from stale_pr_decay import comment_is_trusted as stale_comment_is_trusted  # noqa: E402


def _load_fence():
    spec = importlib.util.spec_from_file_location(
        "factory_revocation_fence_worker_app",
        SCRIPTS / "factory-revocation-fence.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class AppIdentityContractTest(unittest.TestCase):
    """Cross-cutting contract assertions for durable factory worker Apps."""

    def test_trusted_factory_app_slugs_is_exactly_github_actions(self) -> None:
        self.assertEqual(TRUSTED_FACTORY_APP_SLUGS, {"github-actions"})

    def test_trusted_marker_app_slugs_is_exactly_github_actions(self) -> None:
        self.assertEqual(TRUSTED_MARKER_APP_SLUGS, frozenset({"github-actions"}))

    def test_worker_app_slug_rejected_even_with_owner_association(self) -> None:
        comment = {
            "user": {"login": "mark-cordova[bot]"},
            "author_association": "OWNER",
            "body": "<!-- forged marker -->",
            "performed_via_github_app": {"slug": "mark-cordova"},
        }
        self.assertTrue(performed_via_untrusted_app(comment))
        self.assertFalse(comment_is_trusted(comment))
        self.assertEqual(trusted_comment_bodies([comment]), [])
        self.assertFalse(stale_comment_is_trusted(comment))
        fence = _load_fence()
        self.assertFalse(fence.comment_is_trusted(comment))

    def test_github_actions_bot_accepted(self) -> None:
        actions = {
            "user": {"login": "github-actions[bot]"},
            "author_association": "NONE",
            "body": "ok",
            "performed_via_github_app": {"slug": "github-actions"},
        }
        self.assertFalse(performed_via_untrusted_app(actions))
        self.assertTrue(comment_is_trusted(actions))
        self.assertEqual(trusted_comment_bodies([actions]), ["ok"])

    def test_human_owner_accepted(self) -> None:
        owner = {
            "user": {"login": "JoshCLWren"},
            "author_association": "OWNER",
            "body": "owner comment",
        }
        self.assertFalse(performed_via_untrusted_app(owner))
        self.assertTrue(comment_is_trusted(owner))
        self.assertEqual(trusted_comment_bodies([owner]), ["owner comment"])

    def test_native_approve_without_controller_marker_provenance_needs_two_reviewers(self) -> None:
        # No contributor markers recorded -> provenance_complete = False
        # Even with two approvers, producer cannot self-approve
        producer = "48"
        # Producer is in the excluded set
        self.assertFalse(head_has_authorized_approval(
            approvers={producer, "17"},
            contributors=set(),
            producer=producer,
            provenance_complete=False,
        ))
        # Two distinct non-producer, non-contributor reviewers satisfy fail-closed
        self.assertTrue(head_has_authorized_approval(
            approvers={"17", "21"},
            contributors=set(),
            producer=producer,
            provenance_complete=False,
        ))
        # With provenance_complete=True, one eligible reviewer suffices
        self.assertTrue(head_has_authorized_approval(
            approvers={"17"},
            contributors=set(),
            producer=producer,
            provenance_complete=True,
        ))

    def test_approval_can_promote_requires_controller_provenance(self) -> None:
        head = "a" * 40
        contributors = {"48"}
        # Without provenance_complete, approval_can_promote fails even with two eligible
        # because head_has_authorized_approval requires two when provenance incomplete
        self.assertFalse(approval_can_promote(
            reviewer="17",
            reviewed_head=head,
            current_head=head,
            verdict="approve",
            mechanical_gates_passed=True,
            contributors=contributors,
            prior_approvers=set(),
            producer="48",
            provenance_complete=False,
        ))
        # With provenance_complete, one eligible reviewer authorizes
        self.assertTrue(approval_can_promote(
            reviewer="17",
            reviewed_head=head,
            current_head=head,
            verdict="approve",
            mechanical_gates_passed=True,
            contributors=contributors,
            prior_approvers=set(),
            producer="48",
            provenance_complete=True,
        ))

    def test_producer_cannot_self_approve_even_with_provenance_complete(self) -> None:
        head = "a" * 40
        self.assertFalse(approval_can_promote(
            reviewer="48",
            reviewed_head=head,
            current_head=head,
            verdict="approve",
            mechanical_gates_passed=True,
            contributors={"48"},
            prior_approvers=set(),
            producer="48",
            provenance_complete=True,
        ))


class Worker48BootstrapContractTest(unittest.TestCase):
    """Bootstrap and persistence contract for worker 48 (Mark Cordova)."""

    def setUp(self) -> None:
        import factory_worker_github_app as app
        self.app = app

    def test_worker_48_first_name_is_mark_cordova(self) -> None:
        self.assertEqual(self.app.bootstrap_display_name(48), "Mark Cordova")

    def test_collision_fallback_is_exactly_second_call_on_same_seeded_instance(self) -> None:
        self.assertEqual(self.app.collision_fallback_name(48), "Michael Spence")
        self.assertEqual(self.app.faker_names(48, 2), ["Mark Cordova", "Michael Spence"])

    def test_mapping_persists_mark_cordova_and_no_other_worker(self) -> None:
        mapping = json.loads((REPO / ".github/factory-worker-github-apps.json").read_text())
        self.assertEqual(set(mapping["workers"]), {"48"})
        entry = mapping["workers"]["48"]
        self.assertEqual(entry["display_name"], "Mark Cordova")
        self.assertEqual(entry["display_name"], self.app.bootstrap_display_name(48))
        self.assertEqual(entry["app_id"], 5275306)
        self.assertEqual(entry["app_login"], "mark-cordova[bot]")
        self.assertEqual(entry["installation_id"], 170306933)
        self.assertIn("Autonomous ComicPile Factory worker", entry["profile"])
        blob = json.dumps(mapping)
        self.assertNotIn('"46"', blob)
        self.assertNotIn('"72"', blob)

    def test_resolve_credential_path_does_not_re_roll_persisted_display_name(self) -> None:
        """Loading mapping with Mark Cordova must not invoke bootstrap again."""
        mapping = {
            "workers": {
                "48": {
                    "display_name": "Mark Cordova",
                    "app_id": "123",
                    "installation_id": "456",
                    "private_key_secret": "FACTORY_WORKER_48_APP_PRIVATE_KEY",
                    "app_id_secret": "FACTORY_WORKER_48_APP_ID",
                    "installation_id_secret": "FACTORY_WORKER_48_INSTALLATION_ID",
                    "profile": "Autonomous ComicPile Factory worker",
                }
            }
        }
        # bootstrap_display_name should not be called; display_name must remain unchanged
        entry = self.app.worker_entry(mapping, "48")
        self.assertIsNotNone(entry)
        self.assertEqual(entry["display_name"], "Mark Cordova")
        # Verify the mapping still has the original name after any resolution logic
        reloaded_entry = self.app.worker_entry(mapping, "48")
        self.assertEqual(reloaded_entry["display_name"], "Mark Cordova")


class ExpectedWorkersContractTest(unittest.TestCase):
    """Provider-derived worker slots replace the static expected-worker roster."""

    def test_dynamic_worker_slots_are_derivable_without_static_roster(self) -> None:
        """Dynamic provider slots must be derivable without committed roster files."""
        self.assertFalse((REPO / ".github" / "factory-expected-workers.json").is_file())
        module_name = "factory_worker_slots"
        previous = sys.modules.get(module_name)
        sys.path.insert(0, str(SCRIPTS))
        try:
            spec = importlib.util.spec_from_file_location(
                module_name,
                SCRIPTS / "factory_worker_slots.py",
            )
            assert spec and spec.loader
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
            derived = module.slots()
        finally:
            if previous is None:
                sys.modules.pop(module_name, None)
            else:
                sys.modules[module_name] = previous
            if str(SCRIPTS) in sys.path:
                sys.path.remove(str(SCRIPTS))
        self.assertGreater(len(derived), 0)
        self.assertEqual(len({slot.worker for slot in derived}), len(derived))

    def test_worker_48_keeps_app_identity_without_static_expected_worker_file(self) -> None:
        """Worker 48's durable App identity persists independently of slot rosters."""
        mapping = json.loads((REPO / ".github/factory-worker-github-apps.json").read_text())
        self.assertIn("48", mapping["workers"])
        self.assertEqual(mapping["workers"]["48"]["app_login"], "mark-cordova[bot]")


if __name__ == "__main__":
    unittest.main()
