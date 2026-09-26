"""Completeness checks for the archived source-artifact inventory."""

from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path

ROOT = Path(__file__).parents[1]
LEGACY_ROOT = ROOT / "reference" / "legacy-factory"
CLASSIFICATIONS = frozenset({"migrate", "reference", "product-specific", "retire"})


@dataclass(frozen=True)
class InventoryRule:
    pattern: str
    classification: str


# Keep synchronized with the ordered table in MIGRATION_INVENTORY.md.
RULES = (
    InventoryRule(".github/scripts/factory_work_policy_latticery_ref.py", "retire"),
    InventoryRule(".github/scripts/factory_*policy.py", "migrate"),
    InventoryRule(".github/scripts/factory_*controller.py", "migrate"),
    InventoryRule(".github/scripts/factory-revocation-fence.py", "migrate"),
    InventoryRule(".github/scripts/stale_pr_decay.py", "migrate"),
    InventoryRule(".github/scripts/test_factory_*policy.py", "reference"),
    InventoryRule(".github/scripts/test_factory_*controller.py", "reference"),
    InventoryRule(".github/scripts/test_factory_review_thread_gate.py", "reference"),
    InventoryRule(".github/scripts/test_factory_stage5.py", "reference"),
    InventoryRule("tests/test_factory_*.py", "reference"),
    InventoryRule("tests/test_generate_factory_status_dashboard.py", "reference"),
    InventoryRule("tests/fixtures/**", "reference"),
    InventoryRule(".github/workflows/**", "product-specific"),
    InventoryRule(".github/scripts/**", "product-specific"),
    InventoryRule(".github/**", "product-specific"),
    InventoryRule(".agents/**", "product-specific"),
    InventoryRule(".claude/**", "product-specific"),
    InventoryRule(".opencode/**", "product-specific"),
    InventoryRule("prompts/**", "product-specific"),
    InventoryRule("scripts/tests/**", "reference"),
    InventoryRule("scripts/**", "product-specific"),
    InventoryRule("docs/**", "reference"),
)


def _archived_paths() -> list[str]:
    return sorted(
        path.relative_to(LEGACY_ROOT).as_posix()
        for path in LEGACY_ROOT.rglob("*")
        if path.is_file() or path.is_symlink()
    )


def _classification(path: str) -> str | None:
    return next(
        (rule.classification for rule in RULES if fnmatchcase(path, rule.pattern)),
        None,
    )


def test_every_archived_artifact_is_classified() -> None:
    unclassified = [path for path in _archived_paths() if _classification(path) is None]
    assert not unclassified, f"unclassified archived artifacts: {unclassified}"


def test_inventory_uses_only_supported_classifications() -> None:
    invalid = [rule for rule in RULES if rule.classification not in CLASSIFICATIONS]
    assert not invalid
