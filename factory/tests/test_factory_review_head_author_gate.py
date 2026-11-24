"""The merge gate applies the head-author rule through the shared resolver, and fails closed."""

from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / ".github" / "scripts"
sys.path.insert(0, str(SCRIPTS))
policy = importlib.import_module("factory_review_policy")
exclusion = importlib.import_module("factory_review_exclusion")

PR = 7
FIRST, SECOND, HEAD, MAIN = "1" * 40, "2" * 40, "3" * 40, "5" * 40
A, B, C = "zai/glm-4.6", "mistral/devstral-2", "opencode-go/hy4-flash-free"


def load_controller() -> ModuleType:
    path = SCRIPTS / "factory-review-controller.py"
    spec = importlib.util.spec_from_file_location("factory_review_controller_head_author", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def author(model: str, head: str, agent: str = "43") -> str:
    return policy.agent_model_author_marker(pr=PR, head=head, model=model, agent=agent)


def reviewed(model: str, agent: str = "17") -> str:
    return policy.reviewer_model_marker(
        pr=PR, head=HEAD, reviewer_model=model, reviewer_agent=agent
    )


def commit(sha: str, *parents: str, message: str = "work") -> dict[str, object]:
    return {"sha": sha, "parents": [{"sha": p} for p in parents], "commit": {"message": message}}


class World:
    def __init__(self) -> None:
        self.paths: dict[str, object] = {f"pulls/{PR}": {"base": {"ref": "main"}}}

    def push(self, sha: str, parent: str, message: str = "work") -> None:
        self.paths[f"commits/{sha}"] = commit(sha, parent, message=message)

    def sync(self, sha: str, ours: str, *, files: int = 1) -> None:
        self.paths[f"commits/{sha}"] = commit(sha, ours, MAIN)
        self.paths[f"compare/{MAIN}...main"] = {"status": "identical"}
        brought = [{"filename": f"main_{n}.py"} for n in range(files)]
        self.paths[f"compare/{ours}...{sha}"] = {"files": brought}
        self.paths[f"compare/{MAIN}...{sha}"] = {"files": [{"filename": "pr_only.py"}]}

    def fetch(self, path: str) -> object:
        return self.paths.get(path)


@pytest.fixture
def gate(monkeypatch: pytest.MonkeyPatch):
    controller = load_controller()
    world = World()

    def gh_json(args: list[str], **_kw: object) -> object:
        return world.fetch(args[1].split("/", 3)[3])

    monkeypatch.setattr(controller, "gh_json", gh_json)

    def verdict(comments: list[str]) -> object:
        return controller.review_eligibility(PR, HEAD, approvers={"17"}, model_comments=comments)

    return world, verdict


def history() -> list[str]:
    return [author(A, FIRST, "40"), author(B, SECOND, "41"), author(C, HEAD, "42")]


def test_controller_gate_accepts_an_earlier_author_and_rejects_the_head_author(gate) -> None:
    world, verdict = gate
    world.push(HEAD, SECOND)
    assert verdict([*history(), reviewed(A)]).eligible
    assert verdict([*history(), reviewed("zen/hy4-flash")]).status == "self-review"


def test_a_human_record_on_a_clean_sync_does_not_launder_the_author(gate) -> None:
    world, verdict = gate
    world.sync(HEAD, SECOND)
    human = policy.agent_model_author_marker(pr=PR, head=HEAD, model="human", agent="o")
    comments = [author(A, FIRST), author(C, SECOND), human, reviewed("zen/hy4-flash")]
    assert verdict(comments).status == "self-review"
    assert verdict([*comments[:3], reviewed(A)]).eligible


def test_an_unprovable_sync_excludes_every_recorded_author(gate) -> None:
    world, verdict = gate
    world.sync(HEAD, SECOND, files=exclusion.MAX_BASE_MERGE_FILES)
    comments = [author(A, FIRST), author(C, SECOND), reviewed(A)]
    result = verdict(comments)
    assert result.status == "self-review"
    assert set(result.author_models) == {"zai+glm-4.6", "opencode-go+hy4-flash-free"}


def test_more_syncs_than_the_cap_exclude_every_recorded_author() -> None:
    world = World()
    chain = [f"{n:x}" * 40 for n in range(6, 6 + exclusion.MAX_BASE_MERGE_CHAIN + 1)]
    for sha, below in zip(chain, [*chain[1:], SECOND], strict=True):
        world.sync(sha, below)
    found = exclusion.resolve(
        world.fetch, pr=PR, head=chain[0], base_branch="main", comments=[author(C, SECOND)]
    )
    assert found.source == "sync-chain-too-long"
    assert found.excluded == ("opencode-go+hy4-flash-free",)


def test_a_worker_record_outranks_a_human_trailer(gate) -> None:
    world, verdict = gate
    world.push(HEAD, SECOND, message="fix\n\nModel: human")
    pushed = policy.head_contributor_marker(pr=PR, head=HEAD, worker="12", epoch=1)
    comments = [author(A, FIRST), author(C, SECOND), pushed, reviewed(A)]
    assert verdict(comments).status == "self-review"
    decided = policy.head_author_record(
        comments, pr=PR, heads=(HEAD,), trailers={HEAD: {"human"}}
    )
    assert decided[2] == "unrecorded-worker" and len(decided[1]) == 2


def test_an_unreadable_head_fails_closed(gate) -> None:
    _world, verdict = gate
    comments = [author(A, FIRST), reviewed(A)]
    assert verdict(comments).status == "self-review"


def test_an_earlier_author_worker_is_cleared_by_its_model() -> None:
    comments = [*history(), reviewed(A, agent="40")]
    verdict = policy.evaluate_review_eligibility(comments, pr=PR, head=HEAD, approvers={"40"})
    contributors, _complete = policy.head_contributor_provenance(
        [policy.head_contributor_marker(pr=PR, head=FIRST, worker="40", epoch=1)],
        pr=PR,
        head=HEAD,
    )
    assert "40" in contributors
    assert policy.head_has_authorized_approval(
        approvers={"40"}, contributors=contributors, model_cleared=verdict.cleared_agents
    )
