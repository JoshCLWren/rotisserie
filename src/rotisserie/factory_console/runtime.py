"""Read provider, worker and settings metadata from the factory runtime itself.

The operator console never keeps its own provider list, worker roster or
settings table. It loads the same modules the dispatcher overlays into adopter
repositories (``factory/.github/scripts``). Wheels carry a copy of those files
under ``rotisserie/factory_console/factory_runtime``; a source checkout reads
them in place. The dispatcher still validates everything at execution time.
"""

from __future__ import annotations

import functools
import importlib
import json
import os
import subprocess
import sys
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

RUNTIME_MODULES = (
    "factory_provider_registry",
    "factory_worker_slots",
    "factory_manual_model",
    "factory_settings",
    "factory_dispatch_credential_filter",
    "factory_fleet_switch",
    "factory_incident",
    "factory_published_model_catalog",
    "factory_work_policy",
    "factory_rotisserie_adopter",
    "factory_branch_sync",
    "factory_main_health",
    "factory_review_policy",
    "factory_label_policy",
    "factory_adopter",
    "factory_billing_pool",
    "factory_turso_usage",
    "factory_dynamic_outcome",
)

# Every file the wheel must carry, keyed by module name, with its checkout path.
# The entries after the comprehension are not loaded by name; the modules above import them.
RUNTIME_SOURCES = {
    **{name: f"factory/.github/scripts/{name}.py" for name in RUNTIME_MODULES},
    "factory_work_policy_assignment": "factory/.github/scripts/factory_work_policy_assignment.py",
    "factory_work_policy_backlog": "factory/.github/scripts/factory_work_policy_backlog.py",
    "factory_work_policy_base": "factory/.github/scripts/factory_work_policy_base.py",
    "factory_work_policy_candidates": "factory/.github/scripts/factory_work_policy_candidates.py",
    "factory_work_policy_priority": "factory/.github/scripts/factory_work_policy_priority.py",
    "factory_eligibility": "factory/scripts/factory_eligibility.py",
}

# Factory provider keys that the GitHub dispatch accepts under another name.
PROVIDER_ALIASES = {"opencode-go": "opencode-free", "opencode-free": "opencode-free"}


class RuntimeUnavailableError(RuntimeError):
    """The packaged factory runtime metadata could not be loaded."""


def runtime_directory() -> Path:
    packaged = Path(__file__).with_name("factory_runtime")
    if (packaged / "factory_worker_slots.py").is_file():
        return packaged
    checkout = Path(__file__).resolve().parents[3] / "factory" / ".github" / "scripts"
    if (checkout / "factory_worker_slots.py").is_file():
        return checkout
    raise RuntimeUnavailableError(
        "This Rotisserie installation is missing its factory runtime files; reinstall it."
    )


_BOUND_PREFIX: dict[str, str] = {}


def bind_repository(repository: str) -> None:
    """Derive the runtime modules' comment-marker prefix from the target repository.

    An operator's own FACTORY_MARKER_PREFIX wins, as it does in a workflow.
    """
    current = os.environ.get("FACTORY_MARKER_PREFIX")
    if current is not None and current != _BOUND_PREFIX.get("value"):
        return
    prefix = str(module("factory_adopter").marker_prefix(repository))
    if prefix == current:
        return
    os.environ["FACTORY_MARKER_PREFIX"] = _BOUND_PREFIX["value"] = prefix
    module.cache_clear()
    for name in RUNTIME_SOURCES:
        sys.modules.pop(name, None)


@functools.cache
def module(name: str) -> ModuleType:
    if name not in RUNTIME_MODULES:
        raise RuntimeUnavailableError(f"{name} is not a factory runtime module")
    directory = str(runtime_directory())
    inserted = directory not in sys.path
    if inserted:
        sys.path.insert(0, directory)
    try:
        return importlib.import_module(name)
    except ImportError as exc:  # pragma: no cover - packaging defect
        raise RuntimeUnavailableError(f"factory runtime module {name} failed to load") from exc
    finally:
        if inserted:
            sys.path.remove(directory)


@dataclass(frozen=True)
class Provider:
    """One factory provider integration, in operator vocabulary."""

    key: str  # what the operator types and the dispatcher accepts
    source: str  # the factory's internal source key (worker identities, Neon)
    name: str
    model_prefix: str  # exact model IDs for this provider start with this
    secrets: tuple[str, ...]  # GitHub secret names it needs; empty means none
    needs_variable: str = ""  # an Actions variable that must be enabled


def _string(value: object) -> str:
    return value if isinstance(value, str) else ""


def providers() -> list[Provider]:
    """Every provider the factory runtime integrates, discovered not listed."""
    slots = module("factory_worker_slots")
    registry = module("factory_provider_registry")
    credentials = module("factory_dispatch_credential_filter")
    manual = module("factory_manual_model")
    sources: tuple[str, ...] = tuple(str(item) for item in slots.sources())
    specs = registry.PROVIDERS
    result: list[Provider] = []
    for source in sources:
        key = "opencode-go" if source == "opencode-free" else source
        spec = specs.get(source)
        name = _string(getattr(spec, "name", "")) or _NATIVE_NAMES.get(source, source)
        prefix = _model_prefix(manual, source)
        requirements = credentials.source_requirements(source)
        secrets: list[str] = []
        variable = ""
        if source not in credentials.ANONYMOUS and requirements is not None:
            for flag in requirements:
                flag_name = str(flag)
                if flag_name == "FACTORY_OMNIROUTE_ENABLED":
                    variable = flag_name
                elif flag_name.startswith("FACTORY_HAS_"):
                    secrets.append(flag_name.removeprefix("FACTORY_HAS_"))
        result.append(Provider(key, source, name, prefix, tuple(secrets), variable))
    return sorted(result, key=lambda item: item.key)


# Display names for OpenCode-native integrations that have no registry entry.
_NATIVE_NAMES = {
    "opencode-free": "OpenCode Go",
    "openrouter-free": "OpenRouter free models",
    "kilo-auto": "Kilo",
    "omniroute-free": "OmniRoute",
}


def _model_prefix(manual: ModuleType, source: str) -> str:
    """Ask the runtime's own exact-model validator which prefix it accepts.

    Returns "" when the provider only supports automatic model choice.
    """
    spec = module("factory_provider_registry").PROVIDERS.get(source)
    candidates = ["opencode-go", "openrouter", "kilo", _string(getattr(spec, "provider", ""))]
    suffix = ":free" if source == "openrouter-free" else ""
    for candidate in filter(None, candidates):
        try:
            manual.validate_model_syntax(source, f"{candidate}/probe{suffix}")
        except ValueError:
            continue
        return candidate + "/"
    return ""


def resolve_provider(value: str) -> Provider:
    wanted = PROVIDER_ALIASES.get(value, value)
    for provider in providers():
        if provider.key == value or provider.source == wanted:
            return provider
    known = ", ".join(provider.key for provider in providers())
    raise ValueError(f"Unknown provider {value!r}. Available: {known}")


def validate_model(provider: Provider, model: str) -> None:
    """Apply the dispatcher's own exact-model syntax rule before submitting."""
    if model == "auto":
        return
    module("factory_manual_model").validate_model_syntax(provider.source, model)


def worker_sources() -> dict[str, str]:
    """Every valid worker identity (regular and operator overflow) to its source."""
    identities = module("factory_worker_slots").all_identities()
    return {str(worker): str(source) for worker, source in identities.items()}


def provider_for_worker(worker: str) -> Provider | None:
    source = worker_sources().get(worker)
    if source is None:
        return None
    return next((item for item in providers() if item.source == source), None)


def provider_for_source(source: str) -> str:
    """Display name for a factory source key."""
    found = next((item for item in providers() if item.source == source), None)
    return found.name if found else source


def regular_slot_count() -> int:
    return int(module("factory_worker_slots").slot_count({}))


def worker_slot_number(worker: str) -> int | None:
    """One-based slot number for display, or None for an unknown identity."""
    try:
        slot = module("factory_worker_slots").resolve(worker, {})
    except ValueError:
        return None
    return int(slot.index) + 1


@dataclass(frozen=True)
class SettingInfo:
    name: str
    default: str
    title: str
    description: str
    unit: str
    minimum: int
    maximum: int
    group: str


def settings() -> list[SettingInfo]:
    registry = module("factory_settings")
    return [
        SettingInfo(
            str(item.name),
            str(item.default),
            str(item.title),
            str(item.description),
            str(item.unit),
            int(item.minimum),
            int(item.maximum),
            str(item.group),
        )
        for item in registry.SETTINGS
    ]


def validate_setting(name: str, value: str) -> str:
    registry = module("factory_settings")
    if name not in registry.BY_NAME:
        raise ValueError(f"Unknown setting {name}")
    return str(registry.validate(registry.BY_NAME[name], value))


def published_models(provider: Provider) -> list[str]:
    """Models a provider publicly lists, when it has a public catalog."""
    catalog = module("factory_published_model_catalog")
    if provider.source not in catalog.PUBLIC_CATALOGS:
        return []
    payload = catalog.published_catalog(provider.source, timeout=10)
    return [str(item) for item in catalog.catalog_model_ids(provider.source, payload)]


# Queue eligibility ("why") -------------------------------------------------------

type Row = Mapping[str, object]


def setting_integer(name: str, variables: Mapping[str, str]) -> int:
    """A numeric setting as the factory workflows see it (repository variables only)."""
    registry = module("factory_settings")
    env = {registry.REPO_VARS_ENV: json.dumps(dict(variables))}
    return int(registry.integer(name, env=env))


@contextmanager
def _adopter_repository(repository: str) -> Iterator[None]:
    """The adopter modules name their repository from GITHUB_REPOSITORY, as in a workflow."""
    previous = os.environ.get("GITHUB_REPOSITORY")
    os.environ["GITHUB_REPOSITORY"] = repository
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("GITHUB_REPOSITORY", None)
        else:
            os.environ["GITHUB_REPOSITORY"] = previous


# The failures the controller treats as "intake unavailable; hold fresh issues".
INTAKE_ERRORS: tuple[type[Exception], ...] = (
    KeyError,
    OSError,
    RuntimeError,
    ValueError,
    json.JSONDecodeError,
    subprocess.SubprocessError,
)


def intake_verdicts(
    control: object,
    issues: list[Row],
    prs: list[Row],
    *,
    repository: str,
    revision: str,
    now: int,
    pr_wip_limit: int,
    review_backlog_limit: int,
) -> dict[int, tuple[bool, tuple[str, ...]]] | None:
    """Rotisserie's issue-intake verdicts, computed exactly as the controller does.

    Returns None while the adopter lane is legacy (no intake restriction).
    Raises one of ``INTAKE_ERRORS`` when the
    decision cannot be made; the controller then admits no fresh issue.
    """
    adopter = module("factory_rotisserie_adopter")
    with _adopter_repository(repository):
        raw = adopter.intake_verdicts(
            control,
            [dict(row) for row in issues],
            [dict(row) for row in prs],
            revision=revision,
            now=now,
            command=[sys.executable, "-m", "rotisserie"],
            pr_wip_limit=pr_wip_limit,
            review_backlog_limit=review_backlog_limit,
        )
    if raw is None:
        return None
    return {
        int(number): (bool(eligible), tuple(str(reason) for reason in reasons))
        for number, (eligible, reasons) in raw.items()
    }


@dataclass(frozen=True)
class Explanation:
    """Why one issue is or is not queue-eligible, from the dispatcher's own rules."""

    issue: int
    eligible: bool
    reasons: tuple[str, ...]
    labels: tuple[str, ...]
    owning_prs: tuple[int, ...]
    pr_wip: int
    pr_wip_limit: int
    review_backlog: int
    review_backlog_limit: int
    bypasses_wip_limit: bool
    bypasses_saturated_review_backlog: bool
    intake: str  # legacy-unrestricted | authorized | not-authorized
    open_blocker: bool | None
    incident_pause: bool = False


def explain_issue(
    number: int,
    payload: Row,
    *,
    issues: list[Row],
    prs: list[Row],
    intake: set[int] | None,
    open_blocker: bool | None,
    pr_wip_limit: int,
    review_backlog_limit: int,
    incidents: Mapping[int, int] | None = None,
) -> Explanation:
    report = module("factory_work_policy").explain_issue_report(
        number,
        dict(payload),
        issues=[dict(row) for row in issues],
        prs=[dict(row) for row in prs],
        no_diff_attempts={},  # no-diff retries never gate intake (#90)
        intake=intake,
        open_blocker=open_blocker,
        pr_wip_limit=pr_wip_limit,
        review_backlog_limit=review_backlog_limit,
        incidents=incidents,
    )
    gates = report["dynamic_gates"]
    blocker = gates.get("open_dependency_blocker")
    return Explanation(
        issue=int(report["issue"]),
        eligible=bool(report["eligible"]),
        reasons=tuple(str(item) for item in report["reasons"]),
        labels=tuple(str(item) for item in report["labels"]),
        owning_prs=tuple(int(item) for item in report["open_canonical_prs"]),
        pr_wip=int(gates["factory_pr_wip"]),
        pr_wip_limit=int(gates["factory_pr_wip_limit"]),
        review_backlog=int(gates["review_backlog"]),
        review_backlog_limit=int(gates["review_backlog_limit"]),
        bypasses_wip_limit=bool(gates["bypasses_wip_limit"]),
        bypasses_saturated_review_backlog=bool(gates["bypasses_saturated_review_backlog"]),
        intake=str(gates["rotisserie_intake"]),
        open_blocker=None if blocker is None else bool(blocker),
        incident_pause=bool(gates.get("incident_intake_pause")),
    )


@dataclass(frozen=True)
class BacklogPR:
    """A PR counted toward FACTORY_REVIEW_BACKLOG_LIMIT, with the action it needs."""

    number: int
    title: str
    stage: str
    owner: str  # factory owner label; "factory:unowned" when a worker may take it
    conflicted: bool
    action: str  # merge-conflict | ci-repair | review-findings | review

    @property
    def free(self) -> bool:
        return self.owner in ("", "factory:unowned")


def completion_backlog(prs: list[Row]) -> list[BacklogPR]:
    rows = module("factory_work_policy").completion_backlog([dict(row) for row in prs])
    return [
        BacklogPR(
            int(row["number"]),
            str(row["title"]),
            str(row["stage"] or ""),
            str(row["owner"] or ""),
            bool(row["conflicted"]),
            str(row["action"] or "review"),
        )
        for row in rows
    ]


def pr_wip_count(prs: list[Row]) -> int:
    """Factory PRs holding a worker lease (FACTORY_PR_WIP_LIMIT counts these)."""
    return int(module("factory_work_policy").factory_pr_wip_count([dict(row) for row in prs]))


def linked_issue(pr: Row) -> int | None:
    """The issue a PR delivers (branch name, closing keyword, or ``Fix #N`` title)."""
    found = module("factory_work_policy").linked_issue_from_pr(dict(pr))
    return None if found is None else int(found)
