"""Trust and independence boundaries for actual local review ingestion."""
from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest


def importer() -> ModuleType:
    """Load trusted local-review importer."""
    path = Path(__file__).resolve().parents[1] / ".github/scripts/factory_local_review.py"
    spec = importlib.util.spec_from_file_location("factory_local_review_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def evidence() -> dict[str, object]:
    """Return completed distinct read-only lane evidence."""
    return {"lanes": [
        {"identity": "local-alpha-session", "head": "a" * 40, "verdict": "approve",
         "kind": "alpha-acp", "model": "openrouter/stealth/crouton-alpha",
         "state": "complete", "model_confirmed": True, "session": "actual-acp-session",
         "report": "Independent exact-head review found no actionable findings. " * 2 +
                   "FACTORY_GATE_READY"},
        {"identity": "local-codex-session", "head": "a" * 40, "verdict": "approve",
         "kind": "codex", "state": "complete", "model": "gpt-6", "report": "Independent review found no actionable findings. " * 3},
    ]}


def test_actual_distinct_local_lanes_are_accepted() -> None:
    """Actual local identities round-trip through canonical semantic markers."""
    module = importer()
    lanes = module.validate_lanes(evidence(), "a" * 40, {"local-author-session"})
    assert len(lanes) == 2
    for lane in lanes:
        marker = module.policy.review_marker(pr=123, head="a" * 40,
            reviewer=lane["identity"], producer=None, verdict="approve")
        parsed = module.policy.parse_review_marker(marker)
        assert parsed and parsed["reviewer"] == lane["identity"]


@pytest.mark.parametrize(("field", "value"), [
    ("head", "b" * 40), ("verdict", "repair"), ("model", "substitute-model"),
    ("state", "timeout"), ("model_confirmed", False), ("session", ""),
    ("report", "FACTORY_GATE_READY"),
    ("report", "Contradictory findings " * 5 + "FACTORY_GATE_BLOCKED\nFACTORY_GATE_READY"), ("kind", "invented"), ("identity", "77"),
])
def test_invalid_alpha_evidence_is_rejected(field: str, value: object) -> None:
    """Stale, incomplete, substituted and falsely numbered reports fail closed."""
    payload = evidence()
    lanes = payload["lanes"]
    assert isinstance(lanes, list)
    lanes[0][field] = value
    with pytest.raises(ValueError):
        importer().validate_lanes(payload, "a" * 40, set())


def test_contributor_and_duplicate_reviewers_are_rejected() -> None:
    """A prior repairer cannot approve its own later head."""
    module = importer()
    with pytest.raises(ValueError, match="authored"):
        module.validate_lanes(evidence(), "a" * 40, {"local-alpha-session"})
    payload = evidence()
    lanes = payload["lanes"]
    assert isinstance(lanes, list)
    lanes[1]["identity"] = lanes[0]["identity"]
    with pytest.raises(ValueError, match="share"):
        module.validate_lanes(payload, "a" * 40, set())


def test_local_invocation_cannot_attest(monkeypatch: pytest.MonkeyPatch) -> None:
    """An ordinary shell cannot publish trusted local review attestations."""
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    with pytest.raises(RuntimeError, match="trusted Actions"):
        importer().main()


@pytest.mark.parametrize(("field", "value"), [
    ("event", "pull_request"), ("head_branch", "untrusted-branch"),
    ("actor", {"login": "outsider"}), ("path", ".github/workflows/ci.yml"),
])
def test_untrusted_run_cannot_attest(
    monkeypatch: pytest.MonkeyPatch, field: str, value: object,
) -> None:
    """GitHub-authoritative workflow identity is checked before evidence ingestion."""
    module = importer()
    run = {"event": "workflow_dispatch", "head_branch": "main",
           "actor": {"login": "JoshCLWren"},
           "path": ".github/workflows/factory-local-review.yml"}
    run[field] = value
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_RUN_ID", "42")
    monkeypatch.setattr(module.controller, "gh_json", lambda *args, **kwargs: run)
    with pytest.raises(RuntimeError, match="owner-dispatched"):
        module.main()


@pytest.mark.parametrize("mutation", ["owner", "issue", "head", "none"])
def test_live_evidence_is_rechecked_before_attestation(
    monkeypatch: pytest.MonkeyPatch, mutation: str,
) -> None:
    """Actual ingestion refuses moved contracts, heads and newly acquired owners."""
    import json
    from types import SimpleNamespace

    module = importer()
    head = "a" * 40
    payload = evidence()
    payload.update(pr=123, issue=456, head=head, contributors=["local-author-session"],
                   acceptance_reviewer="local-codex-session", acceptance={})
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_RUN_ID", "42")
    monkeypatch.setenv("LOCAL_REVIEW_EVIDENCE", json.dumps(payload))
    monkeypatch.setattr(module.controller, "gh_json", lambda *args, **kwargs: {
        "event": "workflow_dispatch", "head_branch": "main",
        "actor": {"login": "JoshCLWren"}, "path": ".github/workflows/factory-local-review.yml"})
    calls = 0

    def pr_snapshot(*args: object) -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {"headRefOid": "b" * 40 if mutation == "head" and calls > 1 else head,
                "state": "OPEN", "isDraft": False, "headRefName": "local-repair",
                "labels": [{"name": "factory:local" if mutation == "owner" and calls > 1
                            else "factory:unowned"}]}

    issue_reads = 0

    def issue_snapshot(*args: object) -> dict[str, str]:
        nonlocal issue_reads
        issue_reads += 1
        return {"body": "changed" if mutation == "issue" and issue_reads > 1 else "original"}

    monkeypatch.setattr(module.controller, "pr_json", pr_snapshot)
    monkeypatch.setattr(module.controller, "target_json", issue_snapshot)
    monkeypatch.setattr(module.controller, "reconcile_provenance", lambda **kwargs: {"status": "attested"})
    monkeypatch.setattr(module.controller, "review_comment_bodies", lambda *args: [])
    monkeypatch.setattr(module.controller, "model_comment_bodies", lambda *args: [])
    monkeypatch.setattr(module.controller, "linked_issue_mode_for_pr", lambda *args: (456, False))
    monkeypatch.setattr(module.controller.issue_acceptance, "validate", lambda *args, **kwargs: [])
    monkeypatch.setattr(module.controller, "implementation_delivery_gate", lambda *args: {"decision": "pass"})
    monkeypatch.setattr(module.policy, "current_head_author_identity", lambda *args, **kwargs: ("gpt-6", "local-author-session"))
    writes: list[object] = []

    def gh_call(args: list[str]) -> SimpleNamespace:
        if args[:2] == ["issue", "comment"]:
            writes.append(args)
        return SimpleNamespace(stdout="changed.py\n")

    monkeypatch.setattr(module.controller, "run_gh", gh_call)
    monkeypatch.setattr(module.controller, "replace_factory_labels", lambda *args: writes.append(args))
    if mutation == "none":
        module.main()
        assert writes and writes[-1] == (123, "factory:unowned", "factory:ci")
    else:
        with pytest.raises(RuntimeError):
            module.main()
        assert not writes
