#!/usr/bin/env python3
"""Discover live OpenCode catalogs and sync the factory TSV both ways.

The scheduled workflow opens a PR that retires catalog-absent pins and
NVIDIA pins with a sticky #1093 HTTP 410 retirement marker, **and** pins
unused free OpenCode models (cost==0 / free-roster name, not paid Zen)
onto ``.github/free-model-factories.tsv``. Surplus ``big-pickle``
duplicate slots convert first so unique free models do not grow the
roster without bound. Unused ``big-pickle`` is never grown as a first
pin when the roster has none; surplus duplicates stay convert fodder.
Models in the retirement lock or a 410 marker are never silently
re-pinned. NVIDIA catalog adds are out of scope. NVIDIA catalog
presence is ``opencode models nvidia``, never
integrate.api.nvidia.com.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

from factory_model_catalog import (
    FACTORY_PROVIDERS,
    CatalogEntry,
    CatalogUnavailableError,
    ProviderCatalog,
    load_provider_catalogs,
    strip_provider_prefix,
)
from factory_roster import (
    PROTECTED_SOURCES,
    RosterLock,
    RosterRow,
    default_lock_path,
    default_roster_path,
    is_big_pickle,
    load_roster_comments,
    load_roster_lock,
    load_roster_rows,
    next_available_worker_ids,
    opencode_free_display_name,
    opencode_model_is_free,
    openrouter_model_is_free,
    rebalance_schedule_minutes,
    roster_worker_ids,
    surplus_big_pickle_rows,
    sync_roster_lock,
    write_roster_rows,
)


PROVIDER_BY_SOURCE = {
    "opencode-free": "opencode",
    "nvidia": "nvidia",
    "openrouter-free": "openrouter",
}
# Matches the NVIDIA probe in ``free-model-factory-run.yml``.
RETIREMENT_MARKER = "<!-- factory-model-retired-410:v1 -->"
NVIDIA_SOURCE_LINE = "Source: nvidia"
NVIDIA_410_REASON = "NVIDIA HTTP 410 retirement marker on #1093"


@dataclass(frozen=True)
class PinDecision:
    """Keep-or-retire decision for one roster pin."""

    worker: str
    source: str
    model: str
    action: str
    reason: str


@dataclass(frozen=True)
class UnusedFreeModel:
    """A live free OpenCode model that is not currently pinned."""

    provider: str
    model: str
    cost_input: str | None
    cost_output: str | None
    reason: str


@dataclass(frozen=True)
class PinAddition:
    """Add or convert one unused free OpenCode model onto the TSV."""

    worker: str
    source: str
    model: str
    action: str
    previous_model: str | None
    reason: str


@dataclass(frozen=True)
class RetirementPlan:
    """Diff between the factory roster and live OpenCode catalogs."""

    retirements: tuple[PinDecision, ...]
    kept: tuple[PinDecision, ...]
    unused_free: tuple[UnusedFreeModel, ...]
    paid_rejected: tuple[str, ...]
    catalog_sources: dict[str, str]
    additions: tuple[PinAddition, ...] = ()
    locked_unused: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        """Return a JSON-serializable report."""
        return {
            "retirements": [asdict(item) for item in self.retirements],
            "kept": [asdict(item) for item in self.kept],
            "unused_free": [asdict(item) for item in self.unused_free],
            "additions": [asdict(item) for item in self.additions],
            "locked_unused": list(self.locked_unused),
            "paid_rejected": list(self.paid_rejected),
            "catalog_sources": dict(self.catalog_sources),
        }


def classify_opencode_free_eligibility(
    model: str,
    entry: CatalogEntry | None,
) -> bool:
    """Return whether a model may occupy an opencode-free factory lane.

    Name rules match ``validate-free-model-factories.py``. Explicit cost==0
    from a verbose catalog also qualifies. A proven paid cost always loses.

    Args:
        model: Bare OpenCode model id.
        entry: Optional verbose catalog row.

    Returns:
        True when the model is eligible for an opencode-free pin.
    """
    if entry is not None and entry.cost_is_paid:
        return False
    if opencode_model_is_free(model):
        return True
    return bool(entry is not None and entry.cost_is_zero)


def _catalog_for_source(
    source: str,
    catalogs: Mapping[str, ProviderCatalog],
) -> ProviderCatalog | None:
    """Return the OpenCode provider catalog that owns a roster source."""
    provider = PROVIDER_BY_SOURCE.get(source)
    if provider is None:
        return None
    return catalogs.get(provider)


def flatten_github_comments(payload: object) -> tuple[Mapping[str, object], ...]:
    """Flatten one GitHub comments page or a slurped list of pages.

    Args:
        payload: Parsed JSON from ``gh api --paginate`` (one array) or
            ``gh api --paginate --slurp`` (array of page arrays).

    Returns:
        Comment objects in document order.
    """
    if not isinstance(payload, list):
        return ()
    flattened: list[Mapping[str, object]] = []
    for item in payload:
        if isinstance(item, Mapping):
            flattened.append(item)
        elif isinstance(item, list):
            flattened.extend(value for value in item if isinstance(value, Mapping))
    return tuple(flattened)


def nvidia_models_retired_by_410_comments(payload: object) -> frozenset[str]:
    """Return bare NVIDIA model ids marked retired by #1093 410 comments.

    Matching follows the NVIDIA probe in ``free-model-factory-run.yml``:
    the ``factory-model-retired-410:v1`` marker line, ``Source: nvidia``,
    and ``Model: <id>``. Discovery uses the same evidence the probe uses to
    skip a pin, then drops that pin from the roster instead of leaving a
    skip-loop zombie.

    Args:
        payload: GitHub issue-comment JSON (one page or slurped pages).

    Returns:
        Bare model ids the probe would skip as permanently retired.
    """
    models: set[str] = set()
    for comment in flatten_github_comments(payload):
        body = str(comment.get("body") or "")
        lines = body.splitlines()
        if RETIREMENT_MARKER not in lines:
            continue
        if NVIDIA_SOURCE_LINE not in lines:
            continue
        for line in lines:
            if line.startswith("Model: "):
                model = line.removeprefix("Model: ").strip()
                if model:
                    models.add(model)
                break
    return frozenset(models)


def load_nvidia_410_models(path: Path | None) -> set[str]:
    """Load NVIDIA 410-retired model ids from a GitHub comments dump.

    Args:
        path: Optional JSON path produced by the same ``gh api --paginate``
            call the NVIDIA probe uses against issue #1093.

    Returns:
        Bare NVIDIA model ids with a sticky 410 marker.

    Raises:
        OSError: When the comments file cannot be read.
        json.JSONDecodeError: When the comments file is not valid JSON.
    """
    if path is None:
        return set()
    payload: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("retirement comments JSON must be a GitHub comments array")
    return set(nvidia_models_retired_by_410_comments(payload))


def decide_pin(
    row: RosterRow,
    catalogs: Mapping[str, ProviderCatalog],
    retired_models: set[str],
    retired_410_models: set[str] | None = None,
) -> PinDecision:
    """Classify one roster pin against catalogs, lock, and 410 markers.

    Args:
        row: Factory TSV row.
        catalogs: Loaded OpenCode provider catalogs.
        retired_models: Permanently retired model ids from the roster lock.
        retired_410_models: Bare NVIDIA model ids with a #1093 410 marker.

    Returns:
        Keep or retire decision.
    """
    worker = row["worker"]
    source = row["source"]
    model = row["model"]
    retired_410 = retired_410_models or set()
    if model in retired_models:
        return PinDecision(
            worker=worker,
            source=source,
            model=model,
            action="retire",
            reason="permanently retired",
        )
    if source == "nvidia" and model in retired_410:
        return PinDecision(
            worker=worker,
            source=source,
            model=model,
            action="retire",
            reason=NVIDIA_410_REASON,
        )
    if source in PROTECTED_SOURCES:
        return PinDecision(
            worker=worker,
            source=source,
            model=model,
            action="keep",
            reason=f"{source} is not enumerated by OpenCode CLI",
        )
    catalog = _catalog_for_source(source, catalogs)
    if catalog is None:
        return PinDecision(
            worker=worker,
            source=source,
            model=model,
            action="keep",
            reason=f"no {source} OpenCode catalog was loaded; refusing to retire",
        )
    entry = catalog.get(model)
    if entry is None:
        return PinDecision(
            worker=worker,
            source=source,
            model=model,
            action="retire",
            reason=f"absent from opencode models {catalog.provider}",
        )
    if source == "opencode-free" and not classify_opencode_free_eligibility(model, entry):
        return PinDecision(
            worker=worker,
            source=source,
            model=model,
            action="retire",
            reason="present but not opencode-free eligible (paid or non-free id)",
        )
    if source == "openrouter-free" and not openrouter_model_is_free(model):
        return PinDecision(
            worker=worker,
            source=source,
            model=model,
            action="retire",
            reason="openrouter-free pin left the free tier",
        )
    return PinDecision(
        worker=worker,
        source=source,
        model=model,
        action="keep",
        reason=f"present in opencode models {catalog.provider}",
    )


def unused_free_opencode_models(
    catalog: ProviderCatalog | None,
    roster_models: set[str],
) -> tuple[tuple[UnusedFreeModel, ...], tuple[str, ...]]:
    """List live free OpenCode models that are not currently pinned.

    Args:
        catalog: OpenCode provider catalog.
        roster_models: Bare opencode-free pins already on the roster.

    Returns:
        Unused free candidates and paid ids rejected from the proposal list.
    """
    if catalog is None:
        return (), ()
    unused: list[UnusedFreeModel] = []
    paid: list[str] = []
    for entry in catalog.entries:
        if entry.model_id in roster_models:
            continue
        if entry.cost_is_paid:
            paid.append(entry.model_id)
            continue
        if not classify_opencode_free_eligibility(entry.model_id, entry):
            if entry.cost_is_zero is False:
                paid.append(entry.model_id)
            continue
        unused.append(
            UnusedFreeModel(
                provider="opencode",
                model=entry.model_id,
                cost_input=None if entry.cost_input is None else str(entry.cost_input),
                cost_output=None if entry.cost_output is None else str(entry.cost_output),
                reason=(
                    "cost==0"
                    if entry.cost_is_zero
                    else "free-roster name rule"
                ),
            )
        )
    return tuple(unused), tuple(paid)


def plan_additions(
    remaining_rows: Sequence[RosterRow],
    unused: Sequence[UnusedFreeModel],
    *,
    lock: RosterLock | None = None,
    reusable_slots: Sequence[RosterRow] = (),
) -> tuple[PinAddition, ...]:
    """Choose TSV conversions/adds for unused free OpenCode models.

    Surplus ``big-pickle`` duplicates convert first (highest worker id).
    Just-retired workers from the same plan reuse next so a retire+add
    (for example mimo-v2.5-free → mimo-v2.6-flash-free) keeps the freed
    worker id and dispatcher minute instead of growing a new slot while
    the lock still lists that id as expected. New worker ids are
    allocated only when no surplus or just-freed slot remains. Unused
    ``big-pickle`` is never grown or converted onto as a first pin;
    surplus pickle slots remain the convert source for unique frees.
    Models in ``retired_models`` are never re-pinned. Permanently
    retired worker ids stay occupied.

    Args:
        remaining_rows: Roster rows after planned retirements.
        unused: Live free OpenCode models missing from the incoming TSV.
        lock: Optional retirement lock.
        reusable_slots: Rows dropped by this plan's retirements. Eligible
            unused-free adds may convert these slots instead of growing.

    Returns:
        Ordered pin additions apply will write to the TSV.
    """
    retired_models = set(lock["retired_models"]) if lock is not None else set()
    permanently_retired = (
        {int(item) for item in lock["retired_workers"]} if lock is not None else set()
    )
    eligible = [
        item
        for item in unused
        if item.model not in retired_models and not is_big_pickle(item.model)
    ]
    eligible.sort(key=lambda item: item.model)
    convertible = surplus_big_pickle_rows(remaining_rows)
    reusable = [
        row
        for row in reusable_slots
        if int(row["worker"]) not in permanently_retired
    ]
    reusable.sort(key=lambda row: int(row["worker"]))
    occupied = roster_worker_ids(remaining_rows)
    if lock is not None:
        occupied |= permanently_retired
        occupied |= {int(item) for item in lock["expected_workers"]}
    occupied -= {int(row["worker"]) for row in reusable}

    additions: list[PinAddition] = []
    leftover: list[UnusedFreeModel] = []
    convert_index = 0
    reuse_index = 0
    for item in eligible:
        if convert_index < len(convertible):
            slot = convertible[convert_index]
            convert_index += 1
            additions.append(
                PinAddition(
                    worker=slot["worker"],
                    source="opencode-free",
                    model=item.model,
                    action="convert",
                    previous_model=slot["model"],
                    reason=(
                        "convert surplus big-pickle slot to unused free "
                        "OpenCode model"
                    ),
                )
            )
            continue
        if reuse_index < len(reusable):
            slot = reusable[reuse_index]
            reuse_index += 1
            additions.append(
                PinAddition(
                    worker=slot["worker"],
                    source="opencode-free",
                    model=item.model,
                    action="convert",
                    previous_model=slot["model"],
                    reason=(
                        "reuse just-retired worker slot for unused free "
                        "OpenCode model"
                    ),
                )
            )
            occupied.add(int(slot["worker"]))
            continue
        leftover.append(item)

    if leftover:
        new_ids = next_available_worker_ids(occupied, len(leftover))
        for worker_id, item in zip(new_ids, leftover, strict=True):
            additions.append(
                PinAddition(
                    worker=str(worker_id),
                    source="opencode-free",
                    model=item.model,
                    action="add",
                    previous_model=None,
                    reason=(
                        "add unused free OpenCode model "
                        "(no surplus big-pickle slot)"
                    ),
                )
            )
    return tuple(additions)


def _apply_additions(
    rows: Sequence[RosterRow],
    additions: Sequence[PinAddition],
) -> list[RosterRow]:
    """Convert surplus slots and append grown unused-free pins."""
    by_worker = {item.worker: item for item in additions}
    updated: list[RosterRow] = []
    seen: set[str] = set()
    for row in rows:
        addition = by_worker.get(row["worker"])
        if addition is not None and addition.action == "convert":
            updated.append(
                RosterRow(
                    worker=row["worker"],
                    source="opencode-free",
                    model=addition.model,
                    minute=row["minute"],
                    scheduler=row["scheduler"],
                    display_name=opencode_free_display_name(addition.model),
                )
            )
        else:
            updated.append(row)
        seen.add(row["worker"])
    for addition in additions:
        if addition.action != "add" or addition.worker in seen:
            continue
        updated.append(
            RosterRow(
                worker=addition.worker,
                source="opencode-free",
                model=addition.model,
                minute="",
                scheduler="dispatcher",
                display_name=opencode_free_display_name(addition.model),
            )
        )
    return updated


def plan_retirement(
    rows: Sequence[RosterRow],
    catalogs: Mapping[str, ProviderCatalog],
    *,
    lock: RosterLock | None = None,
    retired_410_models: Iterable[str] | None = None,
    add_unused_free: bool = True,
) -> RetirementPlan:
    """Diff roster pins against OpenCode catalogs and NVIDIA 410 markers.

    Args:
        rows: Current factory roster.
        catalogs: Live or fixture catalogs.
        lock: Optional retirement lock for permanent retirements.
        retired_410_models: Bare NVIDIA model ids with a #1093 410 marker.
        add_unused_free: When True (default), unused free OpenCode models
            become TSV additions. Paid Zen is never added.

    Returns:
        Retirement-and-add plan plus unused-free discovery report.
    """
    retired_models = set(lock["retired_models"]) if lock is not None else set()
    retired_410 = {
        str(item).strip() for item in (retired_410_models or ()) if str(item).strip()
    }
    decisions = [
        decide_pin(row, catalogs, retired_models, retired_410) for row in rows
    ]
    retirements = tuple(item for item in decisions if item.action == "retire")
    kept = tuple(item for item in decisions if item.action == "keep")
    roster_free = {
        strip_provider_prefix(row["model"], "opencode")
        for row in rows
        if row["source"] == "opencode-free"
    }
    unused, paid = unused_free_opencode_models(catalogs.get("opencode"), roster_free)
    retired_workers = {item.worker for item in retirements}
    remaining = [row for row in rows if row["worker"] not in retired_workers]
    reusable_slots = [row for row in rows if row["worker"] in retired_workers]
    locked_unused = tuple(
        item.model for item in unused if item.model in retired_models
    )
    additions = (
        plan_additions(
            remaining,
            unused,
            lock=lock,
            reusable_slots=reusable_slots,
        )
        if add_unused_free
        else ()
    )
    return RetirementPlan(
        retirements=retirements,
        kept=kept,
        unused_free=unused,
        paid_rejected=paid,
        catalog_sources={
            provider: catalog.source for provider, catalog in catalogs.items()
        },
        additions=additions,
        locked_unused=locked_unused,
    )


def apply_plan(
    rows: Sequence[RosterRow],
    plan: RetirementPlan,
) -> list[RosterRow]:
    """Return roster rows after mixed retire+add, with minutes rebalanced.

    Args:
        rows: Current factory roster.
        plan: Plan whose retire workers drop and whose additions pin unused
            free OpenCode models (convert surplus big-pickle first).

    Returns:
        Remaining rows with dispatcher minutes balanced to the validator
        ±1 invariant. Converted workers keep their ids and minutes;
        just-retired workers reused by the same plan stay on the roster
        with the new unused-free pin. Grown pins receive new ids after
        the highest occupied/retired worker and a ``SCHEDULE_MINUTES``
        value from rebalance.
    """
    retired_workers = {item.worker for item in plan.retirements}
    converted_workers = {
        item.worker for item in plan.additions if item.action == "convert"
    }
    remaining = [
        row
        for row in rows
        if row["worker"] not in retired_workers or row["worker"] in converted_workers
    ]
    remaining = _apply_additions(remaining, plan.additions)
    return rebalance_schedule_minutes(remaining)


def required_providers(rows: Sequence[RosterRow]) -> tuple[str, ...]:
    """Return OpenCode providers that must be present before retiring pins."""
    needed: list[str] = []
    for source, provider in PROVIDER_BY_SOURCE.items():
        if any(row["source"] == source for row in rows) and provider not in needed:
            needed.append(provider)
    return tuple(needed)


def assert_catalogs_ready(
    rows: Sequence[RosterRow],
    catalogs: Mapping[str, ProviderCatalog],
) -> None:
    """Fail closed when a roster source has no OpenCode catalog.

    Args:
        rows: Current roster.
        catalogs: Loaded catalogs.

    Raises:
        CatalogUnavailableError: When a required provider catalog is missing.
    """
    missing = [
        provider
        for provider in required_providers(rows)
        if provider not in catalogs
    ]
    if missing:
        raise CatalogUnavailableError(
            "missing OpenCode catalog for: " + ", ".join(missing)
        )


def write_github_output(path: Path, plan: RetirementPlan) -> None:
    """Write workflow ``GITHUB_OUTPUT`` keys for the discovery job."""
    lines = [
        f"retire={'true' if plan.retirements else 'false'}",
        f"add={'true' if plan.additions else 'false'}",
        f"retirement_count={len(plan.retirements)}",
        f"add_count={len(plan.additions)}",
        f"unused_free_count={len(plan.unused_free)}",
        f"locked_unused_count={len(plan.locked_unused)}",
        f"paid_rejected_count={len(plan.paid_rejected)}",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _build_parser() -> argparse.ArgumentParser:
    """Return the retirement CLI parser."""
    parser = argparse.ArgumentParser(
        description=(
            "Discover OpenCode catalogs, retire dead factory pins, and add "
            "unused free OpenCode models to the TSV."
        ),
    )
    parser.add_argument("command", choices=("plan", "apply", "discover"))
    parser.add_argument("--catalog-json", type=Path)
    parser.add_argument("--opencode-text", type=Path)
    parser.add_argument("--nvidia-text", type=Path)
    parser.add_argument("--openrouter-text", type=Path)
    parser.add_argument("--roster", type=Path)
    parser.add_argument("--lock", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--github-output", type=Path)
    parser.add_argument(
        "--retirement-comments",
        type=Path,
        help=(
            "Issue #1093 comments JSON from the same gh api --paginate call "
            "the NVIDIA probe uses. Sticky factory-model-retired-410:v1 "
            "nvidia comments retire matching pins even when OpenCode still "
            "lists them."
        ),
    )
    parser.add_argument("--opencode-bin", default="")
    parser.add_argument(
        "--require-cli",
        action="store_true",
        help="Call live opencode models when a fixture/text dump is missing.",
    )
    parser.add_argument(
        "--fail-on-retirement",
        action="store_true",
        help="Exit 1 when dead pins exist (CI fail-closed without applying).",
    )
    parser.add_argument(
        "--add-unused-free",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Pin unused free OpenCode models onto the TSV (default: on). "
            "Paid Zen and retired_models / 410-locked ids are never added."
        ),
    )
    return parser


def _text_paths(args: argparse.Namespace) -> dict[str, Path]:
    """Collect optional per-provider CLI stdout dumps."""
    paths: dict[str, Path] = {}
    if args.opencode_text is not None:
        paths["opencode"] = args.opencode_text
    if args.nvidia_text is not None:
        paths["nvidia"] = args.nvidia_text
    if args.openrouter_text is not None:
        paths["openrouter"] = args.openrouter_text
    return paths


def run(argv: Sequence[str] | None = None) -> int:
    """Run the discovery/retirement CLI.

    Args:
        argv: Optional argument vector.

    Returns:
        Process exit status.
    """
    parser = _build_parser()
    args = parser.parse_args(argv)
    roster_path = args.roster or default_roster_path()
    lock_path = args.lock or default_lock_path()
    rows = load_roster_rows(roster_path)
    lock = load_roster_lock(lock_path if lock_path.is_file() else None)
    require_cli = bool(args.require_cli) or (
        args.catalog_json is None and not _text_paths(args)
    )
    try:
        catalogs = load_provider_catalogs(
            fixture_path=args.catalog_json,
            text_paths=_text_paths(args),
            providers=required_providers(rows) or FACTORY_PROVIDERS,
            require_cli=require_cli,
            opencode_bin=args.opencode_bin or None,
        )
        assert_catalogs_ready(rows, catalogs)
    except CatalogUnavailableError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    try:
        retired_410 = load_nvidia_410_models(args.retirement_comments)
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        print(f"ERROR: cannot read retirement comments: {exc}", file=sys.stderr)
        return 2

    plan = plan_retirement(
        rows,
        catalogs,
        lock=lock,
        retired_410_models=retired_410,
        add_unused_free=bool(args.add_unused_free),
    )
    report = json.dumps(plan.as_dict(), indent=2, sort_keys=True)
    if args.report is not None:
        args.report.write_text(report + "\n", encoding="utf-8")
    else:
        print(report)
    if args.github_output is not None:
        write_github_output(args.github_output, plan)

    should_apply = args.command == "apply" and (
        plan.retirements or plan.additions
    )
    if should_apply:
        before_minutes = {row["worker"]: row["minute"] for row in rows}
        remaining = apply_plan(rows, plan)
        moved = sum(
            1
            for row in remaining
            if row["worker"] in before_minutes
            and row["minute"] != before_minutes[row["worker"]]
        )
        write_roster_rows(roster_path, remaining, load_roster_comments(roster_path))
        reused_workers = {
            int(item.worker)
            for item in plan.additions
            if item.action == "convert"
        }
        sync_roster_lock(
            remaining,
            lock_path=lock_path,
            extra_retired_workers=(
                int(item.worker)
                for item in plan.retirements
                if int(item.worker) not in reused_workers
            ),
            extra_retired_models=(item.model for item in plan.retirements),
        )
        converted = sum(1 for item in plan.additions if item.action == "convert")
        grown = sum(1 for item in plan.additions if item.action == "add")
        print(
            f"Retired {len(plan.retirements)} pin(s); "
            f"added {len(plan.additions)} unused free pin(s) "
            f"({converted} converted, {grown} grown); "
            f"{len(remaining)} roster slot(s) remain; "
            f"reassigned {moved} dispatcher minute(s).",
            file=sys.stderr,
        )

    if args.fail_on_retirement and plan.retirements:
        return 1
    return 0


def main() -> int:
    """CLI entry point."""
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
