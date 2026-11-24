"""``providers`` and ``models`` turn providers and models off or on for the fleet."""

from __future__ import annotations

import base64
import io
import json
from collections.abc import Sequence
from datetime import UTC, datetime

import pytest

from rotisserie import cli, web
from rotisserie.factory_console import commands
from rotisserie.factory_console.github import GitHub, GitHubError
from rotisserie.factory_console.service import catalog, fleet, model_choice
from rotisserie.factory_console.tracker import Clock

REPO = "acme/widgets"
NOW = datetime(2026, 11, 23, 22, 0, tzinfo=UTC)


class Variables(GitHub):
    """Repository variables and secrets in memory; every other read is empty."""

    def __init__(self, values: dict[str, str] | None = None, stats: object = None) -> None:
        super().__init__(REPO)
        self.values = dict(values or {})
        self.stats = stats
        self.writes: list[list[str]] = []

    def _run(
        self, args: Sequence[str], *, stdin: str | None = None, allow_exit_one: bool = False
    ) -> str:
        argv = list(args)
        if argv[:2] == ["variable", "list"]:
            return json.dumps([{"name": k, "value": v} for k, v in self.values.items()])
        if argv[:2] == ["variable", "set"]:
            self.writes.append(argv)
            self.values[argv[2]] = argv[argv.index("--body") + 1]
            return ""
        if argv[:2] == ["variable", "delete"]:
            self.writes.append(argv)
            self.values.pop(argv[2], None)
            return ""
        if argv[:2] == ["secret", "list"]:
            return json.dumps([{"name": "OPENCODE_GO_CLIENT_ID"}, {"name": "NVIDIA_API_KEY"}])
        if argv[0] == "api" and any("factory-stats.json" in item for item in argv):
            return json.dumps(self.stats or {})
        return "[]" if argv[0] != "api" else "{}"


def run(github: Variables, argv: list[str], monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    monkeypatch.setattr(catalog, "opencode_catalog", lambda: [])
    monkeypatch.setattr(catalog, "live_models", lambda provider: None)
    arguments = cli.parser().parse_args(["factory", "--repo", REPO, "--json", *argv])
    out = io.StringIO()
    code = commands.main(
        arguments,
        env={"TERM": "xterm"},
        stdout=out,
        stdin=io.StringIO(),
        clock=Clock(now=lambda: NOW, sleep=lambda _: None),
        executable="sh",
    )
    data: dict[str, object] = json.loads(out.getvalue().strip().splitlines()[-1])
    data["exit_code"] = code
    return data


@pytest.fixture
def github(monkeypatch: pytest.MonkeyPatch) -> Variables:
    found = Variables()
    monkeypatch.setattr(GitHub, "_run", lambda self, args, **kw: Variables._run(found, args))
    return found


def test_disable_local_ollama_writes_the_delivered_list_and_a_private_reason(
    github: Variables, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = run(github, ["providers", "disable", "local-ollama", "--reason", "fans"], monkeypatch)
    assert data["exit_code"] == 0
    assert github.values["FACTORY_DISABLED_PROVIDERS"] == "local-ollama"
    notes = json.loads(github.values["FACTORY_DISABLED_REASONS"])
    assert notes["provider:local-ollama"]["reason"] == "fans"
    listing = run(github, ["providers"], monkeypatch)
    rows = {row["provider"]: row for row in listing["providers"]}  # type: ignore[attr-defined]
    assert rows["local-ollama"]["disabled"] is True
    assert rows["local-ollama"]["disabled_reason"] == "fans"
    assert rows["opencode-go"]["disabled"] is False
    run(github, ["providers", "enable", "local-ollama"], monkeypatch)
    assert "FACTORY_DISABLED_PROVIDERS" not in github.values
    assert "FACTORY_DISABLED_REASONS" not in github.values


def test_provider_alias_is_stored_as_the_factory_source(
    github: Variables, monkeypatch: pytest.MonkeyPatch
) -> None:
    run(github, ["providers", "disable", "opencode-go"], monkeypatch)
    assert github.values["FACTORY_DISABLED_PROVIDERS"] == "opencode-free"
    data = run(github, ["providers", "disable", "opencode-go"], monkeypatch)
    assert data["changed"] is False


def test_unknown_provider_and_bad_usage_are_refused(
    github: Variables, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert run(github, ["providers", "disable", "nope"], monkeypatch)["exit_code"] == 2
    assert run(github, ["providers", "disable"], monkeypatch)["exit_code"] == 2
    assert run(github, ["models", "disable", "opencode-go"], monkeypatch)["exit_code"] == 2
    assert not github.writes


def test_model_disable_adds_the_provider_prefix_shown_by_models(
    github: Variables, monkeypatch: pytest.MonkeyPatch
) -> None:
    run(
        github,
        ["models", "disable", "opencode-go", "hy4-flash-free", "--reason=flaky"],
        monkeypatch,
    )
    assert github.values["FACTORY_DISABLED_MODELS"] == "opencode-free=opencode-go/hy4-flash-free"
    listing = run(github, ["models", "opencode-go"], monkeypatch)
    rows = {row["model"]: row for row in listing["models"]}  # type: ignore[attr-defined]
    assert rows["opencode-go/hy4-flash-free"]["disabled"] is True
    assert rows["opencode-go/hy4-flash-free"]["disabled_reason"] == "flaky"
    run(github, ["models", "enable", "opencode-go", "opencode-go/hy4-flash-free"], monkeypatch)
    assert "FACTORY_DISABLED_MODELS" not in github.values


def stats(*rows: tuple[str, str, int, int]) -> dict[str, str]:
    entries = [
        {
            "source": source,
            "model": model,
            "health": {"status": "healthy"},
            "windows": {"7d": {"runs": runs, "delivered": delivered, "failed": runs - delivered}},
        }
        for source, model, runs, delivered in rows
    ]
    payload = {
        "generated_at": "2026-11-23T21:00:00Z",
        "models": entries,
        "providers": [],
        "model_exclusions": {"generated_at": "2026-11-23T21:50:00Z", "sources": {}},
    }
    return {"content": base64.b64encode(json.dumps(payload).encode()).decode()}


def test_unstick_never_picks_a_disabled_provider_or_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(catalog, "live_models", lambda provider: None)
    snapshot = stats(
        ("opencode-free", "hy4-flash-free", 10, 9),
        ("opencode-free", "nemotron-free", 10, 5),
        ("nvidia", "meta/llama-9", 10, 8),
    )
    values = {
        "FACTORY_DISABLED_PROVIDERS": "nvidia",
        "FACTORY_DISABLED_MODELS": "opencode-free=opencode-go/hy4-flash-free",
    }
    github = Variables(values, snapshot)
    _, ranked, *_ = model_choice.choose_models(github, values, NOW, None, None)
    assert [model for _, model, _ in ranked] == ["opencode-go/nemotron-free"]
    _, named, *_ = model_choice.choose_models(github, values, NOW, "nvidia", None)
    assert [model for _, model, _ in named] == ["nvidia/meta/llama-9"]
    _, unswitched, *_ = model_choice.choose_models(github, {}, NOW, None, None)
    assert len(unswitched) == 3


def test_fleet_state_reads_only_well_formed_entries() -> None:
    state = fleet.state(
        {"FACTORY_DISABLED_PROVIDERS": 'kilo-auto,Bad"', "FACTORY_DISABLED_REASONS": "not json"}
    )
    assert list(state.providers) == ["kilo-auto"]
    assert state.provider_off("kilo-auto") is not None


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (
            {"operation": "provider_switch", "provider": "local-ollama", "reason": "loud fans"},
            ["providers", "disable", "local-ollama", "--reason=loud fans"],
        ),
        (
            {"operation": "provider_switch", "provider": "local-ollama", "enabled": True},
            ["providers", "enable", "local-ollama", "--reason="],
        ),
        (
            {
                "operation": "model_switch",
                "provider": "opencode-go",
                "model": "opencode-go/hy4-flash-free",
                "reason": "out of credits, #234",
            },
            [
                "models",
                "disable",
                "opencode-go",
                "opencode-go/hy4-flash-free",
                "--reason=out of credits, #234",
            ],
        ),
        (
            {
                "operation": "model_switch",
                "provider": "opencode-go",
                "model": "opencode-go/hy4-flash-free",
                "enabled": True,
            },
            ["models", "enable", "opencode-go", "opencode-go/hy4-flash-free", "--reason="],
        ),
    ],
)
def test_web_switch_writes_map_to_cli_commands(
    body: dict[str, object], expected: list[str]
) -> None:
    args = web.write_command(body)
    assert args == expected
    cli.parser().parse_args(["factory", *args])


@pytest.mark.parametrize(
    "body",
    [
        {"operation": "provider_switch", "provider": "--repo=x"},
        {"operation": "provider_switch", "provider": "kilo-auto", "reason": "a\nb"},
        {"operation": "provider_switch", "provider": "kilo-auto", "enabled": "yes"},
        {"operation": "model_switch", "provider": "opencode-go"},
        {"operation": "provider_switch", "provider": "kilo-auto"},
        {"operation": "provider_switch", "provider": "kilo-auto", "reason": "   "},
        {"operation": "model_switch", "provider": "opencode-go", "model": "opencode-go/x"},
    ],
)
def test_web_switch_refuses_unsafe_input(body: dict[str, object]) -> None:
    with pytest.raises(web.InvalidRequest):
        web.write_command(body)


def test_a_failed_variable_read_changes_nothing(
    github: Variables, monkeypatch: pytest.MonkeyPatch
) -> None:
    github.values["FACTORY_DISABLED_PROVIDERS"] = "local-ollama"
    original = Variables._run

    def failing(self: Variables, args: Sequence[str], **_: object) -> str:
        if list(args[:2]) == ["variable", "list"]:
            raise GitHubError("HTTP 502")
        return original(self, args)

    monkeypatch.setattr(GitHub, "_run", lambda self, args, **kw: failing(github, args))
    data = run(github, ["providers", "disable", "kilo-auto"], monkeypatch)
    assert data["exit_code"] == 5
    assert not github.writes
    assert github.values["FACTORY_DISABLED_PROVIDERS"] == "local-ollama"
