"""Is the default branch verified? The serialized merge gate's view, for any front end.

Factory merges are serialized: one merge lands, then the resulting commit must
pass product CI before the next merge. This service reports that state for the
exact default-branch commit using the factory runtime's own classifier, so the
console, a TUI, a web view or an agent all see the verdict the merge drain acts
on. ``unverified`` (no product CI run yet) is never treated as green.

Callers can pass already-fetched data (``sha``, ``runs``, ``variables``) so one
snapshot serves several checks without repeating GitHub reads.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass

from rotisserie.factory_console import runtime
from rotisserie.factory_console.github import JSON, GitHub, GitHubError, obj, string
from rotisserie.factory_console.service import providers

WORKFLOWS_VARIABLE = "FACTORY_MAIN_CI_WORKFLOWS"


@dataclass(frozen=True)
class Run:
    id: int
    workflow: str
    event: str
    status: str
    conclusion: str


@dataclass(frozen=True)
class MainHealth:
    """Product CI verdict for one exact default-branch commit."""

    sha: str
    state: str  # green | pending | red | unverified
    explanation: str
    workflows: tuple[str, ...]
    runs: tuple[Run, ...] = ()
    failing: tuple[str, ...] = ()
    pending: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()

    @property
    def merges_allowed(self) -> bool:
        """Whether a normal (non-repair) merge may land now."""
        return self.state == "green"

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def workflows(github: GitHub, variables: Mapping[str, str] | None = None) -> tuple[str, ...]:
    """The product CI workflow files the adopter configured (default ``ci.yml``)."""
    values = providers.repository_variables(github) if variables is None else variables
    parsed: tuple[str, ...] = runtime.module("factory_main_health").configured_workflows(
        values.get(WORKFLOWS_VARIABLE, "")
    )
    return parsed


def default_branch_sha(github: GitHub) -> str:
    """The exact commit at the head of the default branch."""
    sha = string(obj(github.repo_api("commits/HEAD")), "sha")
    if len(sha) != 40:
        raise GitHubError("could not read the default branch's head commit")
    return sha


def fetch_runs(github: GitHub, sha: str, names: Sequence[str]) -> list[JSON]:
    """One read per configured workflow, filtered to the exact commit."""
    return [
        github.repo_api(f"actions/workflows/{name}/runs?head_sha={sha}&per_page=100")
        for name in names
    ]


def assess(
    github: GitHub,
    *,
    sha: str | None = None,
    runs: Sequence[object] | None = None,
    variables: Mapping[str, str] | None = None,
    names: Sequence[str] | None = None,
) -> MainHealth:
    """Classify the default branch; any pre-fetched input skips its GitHub read."""
    configured = tuple(names) if names else workflows(github, variables)
    target = sha or default_branch_sha(github)
    payload = list(runs) if runs is not None else fetch_runs(github, target, configured)
    try:
        raw = runtime.module("factory_main_health").classify(payload, target, configured)
    except ValueError as exc:
        raise GitHubError(f"unexpected workflow-runs response: {exc}") from exc
    return MainHealth(
        sha=str(raw["sha"]),
        state=str(raw["state"]),
        explanation=str(raw["explanation"]),
        workflows=tuple(raw["workflows"]),
        runs=tuple(
            Run(
                id=int(run["id"]),
                workflow=str(run["workflow"]),
                event=str(run["event"]),
                status=str(run["status"]),
                conclusion=str(run["conclusion"]),
            )
            for run in raw["runs"]
        ),
        failing=tuple(raw["failing"]),
        pending=tuple(raw["pending"]),
        missing=tuple(raw["missing"]),
    )


def dispatch_verification(github: GitHub, health: MainHealth) -> list[str]:
    """Start product CI for an unverified commit; a no-op in any other state.

    Only workflows with no run on that commit are dispatched, so repeating this
    (from doctor, the drain or the dispatcher) never stacks duplicate CI.
    """
    if health.state != "unverified":
        return []
    started: list[str] = []
    for name in health.missing:
        github.gh("workflow", "run", name, "--repo", github.repo)
        started.append(name)
    return started
