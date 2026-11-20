"""Regression coverage for deterministic recovery publication and safe retention."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "checkpoint_handoff", ROOT / ".github/scripts/factory_checkpoint_handoff.py",
)
assert SPEC and SPEC.loader
handoff = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(handoff)
REPO = "JoshCLWren/comic-pile"
RUN = "12345"
HEAD = "a" * 40
BASE = "b" * 40
BRANCH = f"factory/work-checkpoints/issue-4853/kilo-auto-free/{RUN}"


def labels(*names: str) -> list[dict]:
    """Build GitHub label objects."""
    return [{"name": name} for name in names]


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> dict:
    state: dict = {
        "run": {"id": int(RUN), "status": "completed", "display_title": "Factory 46 · fixed-model entry",
                "html_url": f"https://github.com/{REPO}/actions/runs/{RUN}"},
        "saved": {"head": HEAD, "base": BASE, "tree": "c" * 40,
                  "branch": BRANCH, "mode": "issue", "number": 4853,
                  "worker": "46", "issue": "4853", "model": "kilo-auto/free"},
        "drafts": [], "calls": [], "comments": [], "incorporated": False,
        "issue": {"number": 4853, "state": "open", "title": "Implement real work",
                  "body": "Acceptance: deliver it. Do not close #4737. Fixes #999.",
                  "labels": labels("factory", "factory:46", "factory:building")},
        "pr": {"number": 5152, "state": "open", "draft": False,
               "title": "Custom work title", "body": "Custom description", "base": {"ref": "main"},
               "head": {"ref": "factory/98-4853-kilo-auto", "sha": HEAD,
                        "repo": {"full_name": REPO}},
               "labels": labels("factory", "factory:46", "factory:building")},
        "producers": [],
    }
    monkeypatch.setattr(handoff, "verified_run", lambda *_: state["run"])
    monkeypatch.setattr(handoff, "newer_worker_run", lambda *_: False)
    monkeypatch.setattr(handoff, "checkpoint", lambda *_: state["saved"])
    monkeypatch.setattr(handoff, "recovery_prs", lambda *_: state["drafts"])
    monkeypatch.setattr(handoff, "incorporated", lambda *_: state["incorporated"])
    monkeypatch.setattr(handoff, "pages", lambda *_: state["comments"])
    monkeypatch.setattr(handoff, "record_producer", lambda _, pr, saved:
                        state["producers"].append((pr["number"], saved["worker"], pr["head"]["sha"])))

    def obj(endpoint: str) -> dict:
        if "/compare/" in endpoint:
            return {"status": "ahead"}
        if endpoint.endswith("/4853"):
            return state["issue"]
        return state["pr"]

    def api(endpoint: str, method: str = "GET", **fields: object) -> object:
        state["calls"].append((endpoint, method, fields))
        if method == "POST" and endpoint.endswith("/pulls"):
            result = state["pr"]
            result.update({"title": fields["title"], "body": fields["body"], "draft": fields["draft"]})
            result["head"]["ref"] = fields["head"]
            state["drafts"].append(result)
            return result
        if method == "PUT" and endpoint.endswith("/labels"):
            target = state["issue"] if "/4853/" in endpoint else state["pr"]
            values = fields["labels"]
            assert isinstance(values, list)
            target["labels"] = labels(*values)
        if method == "POST" and endpoint.endswith("/comments"):
            state["comments"].append({"body": fields["body"], "user": {"login": "github-actions[bot]"}})
        return None

    monkeypatch.setattr(handoff, "obj", obj)
    monkeypatch.setattr(handoff, "api", api)
    return state


def test_issue_publication_is_actual_normal_scoped_work_pr(harness: dict) -> None:
    result = handoff.publish(REPO, RUN)
    assert result["saved"] and result["published"] and not result["validated"]
    create = next(call[2] for call in harness["calls"] if call[0].endswith("/pulls"))
    assert create["draft"] is False and create["base"] == "main"
    assert create["head"] == BRANCH and create["title"] == "Implement real work"
    assert create["body"].startswith("Closes #4853.")
    assert "close #4737" not in create["body"] and "Fixes #999" not in create["body"]
    assert "Worker: opencode-free-model-factory-46" in create["body"]
    assert "Acceptance: deliver it" in create["body"]
    assert harness["producers"] == [(5152, "46", HEAD)]
    for target in (harness["issue"], harness["pr"]):
        assert handoff.labels_of(target) == {"factory", "factory:unowned",
            "factory:changes-requested"} | ({"ralph-status:in-review"} if target is harness["issue"] else set())


def test_publication_retries_same_pr_without_duplicate_or_overwriting_custom_body(harness: dict) -> None:
    handoff.publish(REPO, RUN)
    harness["pr"]["body"] = "Custom handoff and acceptance"
    harness["pr"]["title"] = "Actual continued work"
    harness["calls"].clear()
    result = handoff.publish(REPO, RUN)
    assert result["work_pr"] == 5152
    assert not any(call[0].endswith("/pulls") or call[1] == "PATCH" for call in harness["calls"])
    assert harness["pr"]["body"] == "Custom handoff and acceptance"


def test_active_assignment_owns_pr_then_source_end_releases_for_repair(harness: dict) -> None:
    harness["run"]["status"] = "in_progress"
    handoff.publish(REPO, RUN)
    assert handoff.labels_of(harness["pr"]) == {"factory", "factory:46", "factory:building"}
    handoff.publish(REPO, RUN, source_ended=True)
    assert handoff.labels_of(harness["pr"]) == {"factory", "factory:unowned", "factory:changes-requested"}
    assert "factory:unowned" in handoff.labels_of(harness["issue"])


@pytest.mark.parametrize("target", ["pr", "issue"])
def test_another_owner_is_never_overwritten(harness: dict, target: str) -> None:
    harness["drafts"] = [harness["pr"]]
    harness[target]["labels"] = labels("factory", "factory:local", "factory:review")
    handoff.publish(REPO, RUN)
    assert handoff.labels_of(harness[target]) == {"factory", "factory:local", "factory:review"}


def test_resumed_same_factory_commit_and_stage_survive_old_run_sweep(harness: dict) -> None:
    harness["drafts"] = [harness["pr"]]
    harness["saved"]["resumed"] = True
    harness["pr"]["labels"] = labels("factory", "factory:46", "factory:review")
    harness["issue"]["labels"] = labels("factory", "factory:46", "factory:review")
    handoff.publish(REPO, RUN, cleanup=True)
    assert not any(call[1] in {"PUT", "DELETE", "PATCH"} for call in harness["calls"])


def test_assigned_repair_uses_original_pr_and_retains_unintegrated_legacy_bytes(harness: dict) -> None:
    harness["saved"]["mode"] = "pr"
    harness["saved"]["number"] = 5152
    harness["pr"]["head"]["sha"] = "d" * 40
    harness["drafts"] = [{"number": 5200, "state": "open"}]
    result = handoff.publish(REPO, RUN, cleanup=True)
    assert result["work_pr"] == 5152
    assert not any(call[0].endswith("/pulls") or call[1] in {"PUT", "PATCH", "DELETE"}
                   for call in harness["calls"])
    assert any(BRANCH in call[2].get("body", "") and HEAD in call[2].get("body", "")
               for call in harness["calls"])


def test_open_canonical_pr_is_never_deleted_even_if_bytes_incorporated(harness: dict) -> None:
    handoff.publish(REPO, RUN)
    harness["incorporated"] = True
    result = handoff.publish(REPO, RUN, cleanup=True)
    assert result["published"]
    assert not any(call[1] == "DELETE" for call in harness["calls"])


def test_no_saved_diff_does_not_create_empty_pr(harness: dict) -> None:
    harness["saved"] = None
    assert handoff.publish(REPO, RUN) == {"saved": False, "published": False, "validated": False}
    assert not harness["calls"]


def test_failed_work_is_retained_and_stays_unvalidated(harness: dict) -> None:
    harness["run"]["conclusion"] = "failure"
    result = handoff.publish(REPO, RUN, cleanup=True)
    assert result["saved"] and result["published"] and result["validated"] is False
    assert not any(call[1] == "DELETE" for call in harness["calls"])


def test_validation_requires_trusted_exact_head_record(harness: dict) -> None:
    handoff.publish(REPO, RUN)
    harness["comments"].append({"body": handoff.validation_marker(RUN, HEAD),
                                "user": {"login": "untrusted"}})
    assert not handoff.validated_head(REPO, harness["pr"], RUN)
    harness["incorporated"] = True
    handoff.record_delivery(REPO, RUN, harness["saved"], 5152)
    assert handoff.validated_head(REPO, harness["pr"], RUN)
    harness["pr"]["head"]["sha"] = "d" * 40
    assert not handoff.validated_head(REPO, harness["pr"], RUN)


def test_incorporation_proves_additions_modes_and_deletions(monkeypatch: pytest.MonkeyPatch) -> None:
    base = {"old": ("old", "100644", "blob"), "edit": ("before", "100644", "blob")}
    saved = {"edit": ("after", "100755", "blob"), "new": ("new", "100644", "blob")}
    target = {**saved, "unrelated": ("other", "100644", "blob")}
    trees = {BASE: base, HEAD: saved, "d" * 40: target}
    monkeypatch.setattr(handoff, "tree", lambda _, sha: trees[sha])
    assert handoff.incorporated(REPO, {"base": BASE, "tree": HEAD}, "d" * 40)
    target["old"] = base["old"]
    assert not handoff.incorporated(REPO, {"base": BASE, "tree": HEAD}, "d" * 40)
    del target["old"]
    target["edit"] = ("after", "100644", "blob")
    assert not handoff.incorporated(REPO, {"base": BASE, "tree": HEAD}, "d" * 40)


@pytest.mark.parametrize("field,value", [
    ("path", ".github/workflows/untrusted.yml"),
    ("event", "pull_request"),
    ("head_branch", "arbitrary-pr-branch"),
    ("id", 98765),
    ("head_repository", {"full_name": "fork/repo"}),
])
def test_foreign_or_untrusted_runs_are_rejected(
    monkeypatch: pytest.MonkeyPatch, field: str, value: object,
) -> None:
    run = {"path": handoff.WORKFLOW, "event": "workflow_dispatch",
           "id": int(RUN), "head_branch": "main",
           "repository": {"full_name": REPO}, "head_repository": {"full_name": REPO}}
    run[field] = value
    monkeypatch.setattr(handoff, "obj", lambda *_: run)
    with pytest.raises(ValueError, match="trusted"):
        handoff.verified_run(REPO, RUN)


def test_named_and_legacy_checkpoint_metadata_remain_recoverable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    refs = [{"ref": f"refs/heads/{BRANCH}", "object": {"sha": HEAD}}]
    message = (f"checkpoint(factory): worker 46 run {RUN} pr #5152 (unreviewed recovery only)\n\n"
               f"Issue: 4853\nModel: kilo-auto/free\nBase-commit: {BASE}\n")
    monkeypatch.setattr(handoff, "pages", lambda *_: refs)
    monkeypatch.setattr(handoff, "obj", lambda path: (
        {"status": "ahead"} if "/compare/" in path else
        {"message": message, "tree": {"sha": "c" * 40}, "parents": [{"sha": BASE}]}
        if path.endswith(HEAD) else {"message": "base"}
    ))
    saved = handoff.checkpoint(REPO, RUN)
    assert saved["branch"] == BRANCH and saved["issue"] == "4853"
    refs[0]["ref"] = f"refs/heads/{handoff.PREFIX}{RUN}"
    message = message.split("\n\n")[0]
    saved = handoff.checkpoint(REPO, RUN)
    assert saved["base"] == BASE
    assert saved["branch"] == f"{handoff.PREFIX}{RUN}"


def test_workflow_and_entry_keep_recovery_independent_and_failure_visible() -> None:
    workflow = (ROOT / ".github/workflows/factory-checkpoint-reconcile.yml").read_text()
    assert "workflow_run:" in workflow and "schedule:" in workflow
    assert "ref: main" in workflow and "persist-credentials: false" in workflow
    assert "--cleanup" in workflow and "--sweep" in workflow
    entry = (ROOT / ".github/workflows/free-model-factory-entry.yml").read_text()
    assert "--outcome" in entry
    assert "Code saved on the work PR; validation incomplete; worker failed:" in entry
    assert 'if [[ "$verdict" == failed ]]; then' in entry


def test_failed_publication_still_records_saved_code_without_masking_failure(
    harness: dict, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    def broken(*_args: object, **_kwargs: object) -> dict:
        raise RuntimeError("transient GitHub failure")

    monkeypatch.setattr(handoff, "publish", broken)
    path = tmp_path / "outcome.json"
    path.write_text(json.dumps({"outcome_class": "work_failure", "delivery_outcome": "none"}))
    monkeypatch.setattr(sys, "argv", ["handoff", "--repo", REPO, "--run-id", RUN,
                                     "--outcome", str(path)])
    assert handoff.main() == 1
    result = json.loads(path.read_text())
    assert result["outcome_class"] == "work_failure" and result["delivery_outcome"] == "none"
    assert result["checkpoint_saved"] is True
    assert result["checkpoint_published"] is False
    assert result["checkpoint_validated"] is False
    assert result["checkpoint_sha"] == HEAD


def test_empty_sweep_is_a_successful_noop(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(handoff, "pages", lambda *_: [])
    monkeypatch.setattr(sys, "argv", ["handoff", "--repo", REPO, "--sweep"])
    assert handoff.main() == 0
    assert json.loads(capsys.readouterr().out) == []


def test_ordinary_commit_tip_keeps_immutable_origin_and_never_resets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    later = 'e' * 40
    origin = (f'checkpoint(factory): worker 46 run {RUN} issue #4853 '
              f'(persisted work; validation pending)\n\nIssue: 4853\n'
              f'Model: kilo-auto/free\nBase-commit: {BASE}\n')
    monkeypatch.setattr(handoff, 'pages', lambda *_: [
        {'ref': f'refs/heads/{BRANCH}', 'object': {'sha': later}},
    ])
    def read(path: str) -> dict:
        if '/compare/' in path:
            return {'status': 'ahead'}
        if path.endswith(later):
            return {'sha': later, 'message': 'Continue implementation normally',
                    'tree': {'sha': later}, 'parents': [{'sha': HEAD}]}
        return {'sha': HEAD, 'message': origin, 'parents': [{'sha': BASE}]}
    monkeypatch.setattr(handoff, 'obj', read)
    saved = handoff.checkpoint(REPO, RUN)
    assert saved is not None
    assert saved['head'] == later and saved['base'] == BASE and saved['resumed'] is True
    assert saved['origin_head'] == HEAD


@pytest.mark.parametrize('change', ['completed', 'foreign-worker', 'owner', 'hold', 'pr-owner'])
def test_writer_fence_checks_actual_source_and_canonical_pr_ownership(
    harness: dict, change: str,
) -> None:
    harness['run']['status'] = 'in_progress'
    harness['drafts'] = [harness['pr']]
    assert handoff.check_write(REPO, RUN, 'issue', 4853, '46', BRANCH)
    if change == 'completed':
        harness['run']['status'] = 'completed'
    elif change == 'foreign-worker':
        harness['run']['display_title'] = 'Factory 42 · fixed-model entry'
    elif change == 'hold':
        harness['issue']['labels'].append({'name': 'do-not-merge'})
    else:
        target = 'pr' if change == 'pr-owner' else 'issue'
        harness[target]['labels'] = labels('factory', 'factory:42', 'factory:building')
    assert not handoff.check_write(REPO, RUN, 'issue', 4853, '46', BRANCH)


def test_tracking_ref_validation_accepts_normal_descendant_on_original_pr(harness: dict) -> None:
    harness['saved']['mode'] = 'pr'
    harness['saved']['number'] = 5152
    harness['pr']['head']['sha'] = 'd' * 40
    harness['incorporated'] = True
    result = handoff.record_delivery(REPO, RUN, harness['saved'], 5152)
    assert result['validated'] and result['work_pr'] == 5152
    assert handoff.validated_head(REPO, harness['pr'], RUN)


def test_newer_same_factory_execution_protects_old_head_and_stage(
    harness: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness['drafts'] = [harness['pr']]
    monkeypatch.setattr(handoff, 'newer_worker_run', lambda *_: True)
    result = handoff.publish(REPO, RUN, cleanup=True)
    assert 'later factory execution' in result['stage']
    assert not any(call[1] in {'PUT', 'DELETE', 'PATCH'} for call in harness['calls'])


@pytest.mark.parametrize('newer,expected', [(True, True), (False, False)])
def test_source_execution_fence_uses_trusted_workflow_run_metadata(
    monkeypatch: pytest.MonkeyPatch, newer: bool, expected: bool,
) -> None:
    run = {'id': int(RUN)}
    runs = [{'id': int(RUN), 'display_title': 'Factory 46 · fixed-model entry'}]
    if newer:
        runs.insert(0, {'id': int(RUN) + 1, 'display_title': runs[0]['display_title']})
    monkeypatch.setattr(handoff, 'obj', lambda *_: {'workflow_runs': runs})
    assert handoff.newer_worker_run(REPO, run, '46') is expected


def test_completed_failed_source_with_missing_stage_becomes_repair_pickup(harness: dict) -> None:
    harness['drafts'] = [harness['pr']]
    harness['issue']['labels'] = labels('factory', 'factory:unowned')
    harness['pr']['labels'] = labels('factory', 'factory:unowned')
    handoff.publish(REPO, RUN)
    assert handoff.labels_of(harness['pr']) == {'factory', 'factory:unowned', 'factory:changes-requested'}
    assert 'factory:unowned' in handoff.labels_of(harness['issue'])
