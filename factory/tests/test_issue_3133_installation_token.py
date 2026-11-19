#!/usr/bin/env python3
"""Acceptance test for issue #3133: worker 48 push + PR creation via installation token.

Verifies the closure-critical contract:

* Worker 48 (Mark Cordova) with all three App secrets configured resolves
  push/pull-request creation to ``source == "installation"`` with a minted
  short-lived token. That ``(source, token)`` pair is what
  ``factory_push_credential`` feeds to both ``git push`` and ``gh pr create``
  in ``_persist_issue_pr_untraced``.
* Worker 48 missing the private key fails closed to ``pr_rebase`` (never fails
  open onto the installation path). When IDs come from saved mapping
  configuration, missing the corresponding env vars does not force fallback.
* When the mapping is unconfigured for IDs, missing any required secret causes
  fallback to ``pr_rebase``.
* Every other worker stays on the shared ``PR_REBASE_TOKEN`` producer.
* The persisted worker-App mapping contains only worker 48.
* The worker App is not a trusted marker author.
"""
from __future__ import annotations

import importlib
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

_SCRIPTS = Path(__file__).resolve().parents[1] / ".github" / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

app = importlib.import_module("factory_worker_github_app")
factory_work_policy = importlib.import_module("factory_work_policy")

REPO = _SCRIPTS.parents[1]
MAPPING_PATH = REPO / ".github" / "factory-worker-github-apps.json"

SHARED_TOKEN = "shared-token"
MINT_STUB_TOKEN = "installation-token-stub"
WORKER_48 = "48"


def _base_env() -> dict[str, str]:
    return {"PR_REBASE_TOKEN": SHARED_TOKEN, "FACTORY_WORKER": "11"}


def _configured_48_env() -> dict[str, str]:
    return {
        **_base_env(),
        "FACTORY_WORKER": WORKER_48,
        "FACTORY_WORKER_48_APP_ID": "123",
        "FACTORY_WORKER_48_INSTALLATION_ID": "456",
        "FACTORY_WORKER_48_APP_PRIVATE_KEY": app.TEST_KEY_SENTINEL,
    }


def _unconfigured_mapping() -> dict[str, object]:
    mapping = json.loads(MAPPING_PATH.read_text(encoding="utf-8"))
    entry = mapping["workers"][WORKER_48]
    entry.update({"app_id": None, "app_login": None, "installation_id": None})
    return mapping


class Worker48InstallationTokenTest(unittest.TestCase):
    """resolve_push_credential is the credential source for push and PR creation."""

    def test_worker_48_with_all_three_secrets_uses_installation_token(self) -> None:
        """All three FACTORY_WORKER_48_* secrets => installation token."""
        env = _configured_48_env()
        with mock.patch.dict(
            os.environ,
            {"FACTORY_WORKER_APP_MINT_STUB_TOKEN": MINT_STUB_TOKEN},
            clear=False,
        ):
            with mock.patch.object(
                app, "load_mapping", return_value=_unconfigured_mapping()
            ):
                source, token = app.resolve_push_credential(env)
        self.assertEqual(source, "installation")
        self.assertEqual(token, MINT_STUB_TOKEN)

    def test_worker_48_missing_app_id_falls_back_to_shared_token(self) -> None:
        """Missing FACTORY_WORKER_48_APP_ID fails closed to PR_REBASE_TOKEN when unconfigured."""
        env = {**_configured_48_env()}
        del env["FACTORY_WORKER_48_APP_ID"]
        with mock.patch.object(
            app, "load_mapping", return_value=_unconfigured_mapping()
        ):
            source, token = app.resolve_push_credential(env)
        self.assertEqual((source, token), ("pr_rebase", SHARED_TOKEN))

    def test_worker_48_missing_installation_id_falls_back_to_shared_token(
        self,
    ) -> None:
        """Missing FACTORY_WORKER_48_INSTALLATION_ID fails closed to PR_REBASE_TOKEN when unconfigured."""
        env = {**_configured_48_env()}
        del env["FACTORY_WORKER_48_INSTALLATION_ID"]
        with mock.patch.object(
            app, "load_mapping", return_value=_unconfigured_mapping()
        ):
            source, token = app.resolve_push_credential(env)
        self.assertEqual((source, token), ("pr_rebase", SHARED_TOKEN))

    def test_worker_48_missing_app_id_env_ok_with_saved_mapping(self) -> None:
        """Missing FACTORY_WORKER_48_APP_ID in env is OK when mapping has saved IDs."""
        env = {**_configured_48_env()}
        del env["FACTORY_WORKER_48_APP_ID"]
        with mock.patch.dict(
            os.environ,
            {"FACTORY_WORKER_APP_MINT_STUB_TOKEN": MINT_STUB_TOKEN},
            clear=False,
        ):
            source, token = app.resolve_push_credential(env)
        self.assertEqual(source, "installation")
        self.assertEqual(token, MINT_STUB_TOKEN)

    def test_worker_48_missing_installation_id_env_ok_with_saved_mapping(self) -> None:
        """Missing FACTORY_WORKER_48_INSTALLATION_ID in env is OK when mapping has saved IDs."""
        env = {**_configured_48_env()}
        del env["FACTORY_WORKER_48_INSTALLATION_ID"]
        with mock.patch.dict(
            os.environ,
            {"FACTORY_WORKER_APP_MINT_STUB_TOKEN": MINT_STUB_TOKEN},
            clear=False,
        ):
            source, token = app.resolve_push_credential(env)
        self.assertEqual(source, "installation")
        self.assertEqual(token, MINT_STUB_TOKEN)

    def test_worker_48_missing_private_key_falls_back_to_shared_token(
        self,
    ) -> None:
        """Missing FACTORY_WORKER_48_APP_PRIVATE_KEY fails closed to PR_REBASE_TOKEN."""
        env = {**_configured_48_env()}
        del env["FACTORY_WORKER_48_APP_PRIVATE_KEY"]
        with mock.patch.object(
            app, "load_mapping", return_value=_unconfigured_mapping()
        ):
            source, token = app.resolve_push_credential(env)
        self.assertEqual((source, token), ("pr_rebase", SHARED_TOKEN))

    def test_other_workers_stay_on_shared_producer(self) -> None:
        """No other worker ever moves off PR_REBASE_TOKEN for worker 48's App."""
        configured = _configured_48_env()
        with mock.patch.object(
            app, "load_mapping", return_value=_unconfigured_mapping()
        ):
            for worker in ("11", "46", "72", ""):
                with self.subTest(worker=worker):
                    source, token = app.resolve_push_credential(
                        {**configured, "FACTORY_WORKER": worker}
                    )
                    self.assertEqual((source, token), ("pr_rebase", SHARED_TOKEN))

    def test_missing_shared_token_fails_closed(self) -> None:
        """No installation and no PR_REBASE_TOKEN raises rather than failing open."""
        with self.assertRaisesRegex(RuntimeError, "PR_REBASE_TOKEN is required"):
            app.resolve_push_credential({"FACTORY_WORKER": WORKER_48})

    def test_mapping_contains_only_worker_48(self) -> None:
        """The persisted mapping is worker 48 only; no other worker is provisioned."""
        mapping = json.loads(MAPPING_PATH.read_text(encoding="utf-8"))
        self.assertEqual(set(mapping["workers"]), {WORKER_48})
        entry = mapping["workers"][WORKER_48]
        self.assertEqual(entry["display_name"], "Mark Cordova")
        self.assertEqual(entry["app_id"], 5275306)
        self.assertEqual(entry["installation_id"], 170306933)
        self.assertIn("Autonomous ComicPile Factory worker", entry["profile"])

    def test_worker_app_is_not_a_trusted_marker_author(self) -> None:
        """Worker 48's App can push/create PRs but is never a trusted marker author."""
        self.assertEqual(factory_work_policy.TRUSTED_FACTORY_APP_SLUGS, {"github-actions"})


if __name__ == "__main__":
    unittest.main()
