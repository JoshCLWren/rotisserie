#!/usr/bin/env python3
"""Operator-tunable factory settings: one registry, one lookup order.

Every number the factory uses to pace itself is declared here with a plain
description, its default and its safe range. A value is resolved in order:

1. an explicit process environment variable (tests, local runs);
2. the adopter repository's Actions variable of the same name, delivered to
   every factory workflow as ``FACTORY_REPO_VARS: ${{ toJSON(vars) }}``;
3. the default below.

``rotisserie factory settings`` lists and changes these values by writing the
repository variables, so a change applies to the next workflow run without a
commit. Invalid values are ignored with a warning and the default applies.

Operator force (``--force``) is never limited by these pacing settings.
"""
from __future__ import annotations

import argparse
import functools
import json
import os
import sys
from collections.abc import Mapping
from dataclasses import asdict, dataclass

REPO_VARS_ENV = "FACTORY_REPO_VARS"


@dataclass(frozen=True)
class Setting:
    name: str
    default: int | str
    title: str
    description: str
    unit: str = "count"
    minimum: int = 1
    maximum: int = 1_000_000
    group: str = "pacing"

    def as_record(self) -> dict[str, object]:
        return asdict(self)


# Work categories in the order the factory picks them up by default. Names are
# operator vocabulary; factory_work_policy maps issue labels onto them.
PRIORITY_CATEGORIES = (
    "main-breakage",   # main or production is broken (main-breakage, deploy-fix)
    "user-bug",        # a user-reported bug
    "user-bug-pr",     # a pull request fixing a user-reported bug
    "normal",          # everything else
    "e2e",             # problems found by end-to-end tests
    "infrastructure",  # factory and infrastructure work
)
DEFAULT_PRIORITY_ORDER = ",".join(PRIORITY_CATEGORIES)

SETTINGS: tuple[Setting, ...] = (
    Setting(
        "FACTORY_SLOTS_PER_PROVIDER", 12, "Workers per provider",
        "How many workers each provider may run at the same time.",
        maximum=99, group="capacity",
    ),
    Setting(
        "FACTORY_PR_WIP_LIMIT", 8, "Open pull request limit",
        "New issues are not started while this many factory pull requests are open.",
        group="capacity",
    ),
    Setting(
        "FACTORY_REVIEW_BACKLOG_LIMIT", 6, "Review backlog limit",
        "New issues are not started while this many pull requests wait for review or repair.",
        group="capacity",
    ),
    Setting(
        "FACTORY_OMNIROUTE_FREE_ENTRY_CAP", 3, "OmniRoute worker limit",
        "How many workers may share the OmniRoute free pool at once.",
        group="capacity",
    ),
    Setting(
        "FACTORY_NO_DIFF_RETRY_LIMIT", 4, "Attempts without changes",
        "After this many attempts that produced no code, an item is paused.",
        group="retries",
    ),
    Setting(
        "FACTORY_NO_DIFF_RETRY_RESET_SECONDS", 86400, "Attempt counter reset",
        "How long before attempts without changes stop counting against an item.",
        unit="seconds", minimum=60, group="retries",
    ),
    Setting(
        "FACTORY_REVIEW_FAILURE_COOLDOWN_SECONDS", 1800, "Wait after a failed review",
        "How long a pull request waits after a failed review before another try.",
        unit="seconds", group="retries",
    ),
    Setting(
        "FACTORY_FIXED_LEASE_TTL_SECONDS", 7500, "Cloud worker claim duration",
        "How long a GitHub-hosted worker keeps its claim if it crashes without releasing it.",
        unit="seconds", minimum=600, group="claims",
    ),
    Setting(
        "FACTORY_LOCAL_LEASE_TTL_SECONDS", 86400, "Local worker claim duration",
        "How long a local worker keeps its claim if it stops reporting progress.",
        unit="seconds", minimum=600, group="claims",
    ),
    Setting(
        "FACTORY_PRIORITY_ORDER", DEFAULT_PRIORITY_ORDER, "Priority order",
        "Which kinds of work are picked first, as a comma-separated list of: "
        + ", ".join(PRIORITY_CATEGORIES) + ".",
        unit="order", minimum=0, maximum=0, group="priority",
    ),
)
BY_NAME: Mapping[str, Setting] = {setting.name: setting for setting in SETTINGS}
# Fleet switch lists (factory_fleet_switch) and incidents (factory_incident): delivered with
# the settings, never validated as numbers.
SWITCHES = ("FACTORY_DISABLED_PROVIDERS", "FACTORY_DISABLED_MODELS", "FACTORY_INCIDENTS")


def _warn(message: str) -> None:
    print(f"[factory-settings] {message}", file=sys.stderr)


def workflow_env_value() -> str:
    """The ``FACTORY_REPO_VARS`` value every factory workflow sets.

    Only registered settings are passed (never every repository variable, which
    would print unrelated configuration into public workflow logs). Unset
    variables arrive as empty strings and are treated as unset.
    """
    names = [setting.name for setting in SETTINGS] + list(SWITCHES)
    pairs = ", ".join(f'"{name}": "${{{{ vars.{name} }}}}"' for name in names)
    return "{" + pairs + "}"


def repository_variables(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return repository variables delivered to this workflow run, if any."""
    values = os.environ if env is None else env
    raw = values.get(REPO_VARS_ENV, "")
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError:
        _warn(f"{REPO_VARS_ENV} is not JSON; using defaults")
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {
        str(key): str(value)
        for key, value in parsed.items()
        if isinstance(value, (str, int)) and str(value) != ""
    }


def raw_value(name: str, env: Mapping[str, str] | None = None) -> tuple[str | None, str]:
    """Return the configured raw value and where it came from."""
    values = os.environ if env is None else env
    if name in values:
        return values[name], "environment"
    variables = repository_variables(env)
    if name in variables:
        return variables[name], "repository variable"
    return None, "default"


def validate(setting: Setting, raw: str) -> int | str:
    """Return the parsed value or raise ValueError with an operator message."""
    if setting.unit == "order":
        order = [part.strip() for part in raw.split(",") if part.strip()]
        unknown = [part for part in order if part not in PRIORITY_CATEGORIES]
        if unknown:
            raise ValueError(f"unknown priority categories: {', '.join(unknown)}")
        if len(set(order)) != len(order):
            raise ValueError("each priority category may appear only once")
        missing = [part for part in PRIORITY_CATEGORIES if part not in order]
        return ",".join(order + missing)
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{setting.title} must be a whole number") from None
    if not setting.minimum <= value <= setting.maximum:
        raise ValueError(
            f"{setting.title} must be between {setting.minimum} and {setting.maximum}"
        )
    return value


def resolve(name: str, env: Mapping[str, str] | None = None) -> tuple[int | str, str]:
    setting = BY_NAME[name]
    raw, origin = raw_value(name, env)
    if raw is None:
        return setting.default, "default"
    try:
        return validate(setting, raw), origin
    except ValueError as exc:
        _warn(f"ignoring {name}={raw!r} ({exc}); using {setting.default}")
        return setting.default, "default (configured value invalid)"


def integer(name: str, default: int | None = None, env: Mapping[str, str] | None = None) -> int:
    """Resolve a numeric setting. Unregistered names keep legacy env semantics."""
    if name not in BY_NAME:
        values = os.environ if env is None else env
        fallback = 0 if default is None else default
        raw = values.get(name, repository_variables(env).get(name))
        if raw is None:
            return fallback
        try:
            value = int(raw)
        except ValueError:
            _warn(f"ignoring non-numeric {name}={raw!r}; using {fallback}")
            return fallback
        if value <= 0:
            _warn(f"ignoring non-positive {name}={raw!r}; using {fallback}")
            return fallback
        return value
    value, _origin = resolve(name, env)
    if not isinstance(value, int):
        raise TypeError(f"{name} is not a numeric setting")
    return value


def priority_ranks(env: Mapping[str, str] | None = None) -> dict[str, int]:
    """Map each work category to its pick-up rank (0 is picked first)."""
    values = os.environ if env is None else env
    return dict(_priority_ranks(values.get("FACTORY_PRIORITY_ORDER"), values.get(REPO_VARS_ENV, "")))


@functools.lru_cache(maxsize=16)
def _priority_ranks(explicit: str | None, delivered: str) -> tuple[tuple[str, int], ...]:
    # Sorting calls this per comparison; parse each configuration once.
    env = {REPO_VARS_ENV: delivered} if explicit is None else {"FACTORY_PRIORITY_ORDER": explicit}
    value, _origin = resolve("FACTORY_PRIORITY_ORDER", env)
    return tuple((category, index) for index, category in enumerate(str(value).split(",")))


def report(env: Mapping[str, str] | None = None) -> list[dict[str, object]]:
    rows = []
    for setting in SETTINGS:
        value, origin = resolve(setting.name, env)
        rows.append(setting.as_record() | {"value": value, "origin": origin})
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("list", "get", "validate"))
    parser.add_argument("--name")
    parser.add_argument("--value")
    args = parser.parse_args()
    if args.command == "list":
        print(json.dumps(report(), sort_keys=True))
        return 0
    if not args.name or args.name not in BY_NAME:
        parser.error("--name must be a registered setting")
    if args.command == "get":
        value, origin = resolve(args.name)
        print(json.dumps({"name": args.name, "value": value, "origin": origin}))
        return 0
    if args.value is None:
        parser.error("validate requires --value")
    try:
        print(json.dumps({"name": args.name, "value": validate(BY_NAME[args.name], args.value)}))
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    with __import__("contextlib").suppress(ImportError):
        __import__("factory_crash").install()
    raise SystemExit(main())
