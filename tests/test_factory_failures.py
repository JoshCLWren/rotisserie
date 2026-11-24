from __future__ import annotations

import io
import json
from datetime import UTC, datetime
from typing import Any, cast

import pytest

from rotisserie import web
from rotisserie.factory_console.github import GitHub
from rotisserie.factory_console.service import failures
from rotisserie.factory_console.tracker import Tracker

REPO = "acme/widgets"
PICK = "Select execution candidate at dispatch time"
PERSIST = "Persist trusted worker outcome in Neon"
REVIEW_ERROR = (
    "No reviewer model: 1 model(s) outside exclusions are eligible and all 1 authored this PR "
    "(2 author model(s) recorded); the review waits for a non-author model"
)


class Hub:
    """Worker runs, jobs, annotations and persist logs, as the Actions API returns them."""

    repo = REPO

    def __init__(self) -> None:
        self.routes: list[str] = []
        self.runs = [
            {
                "id": 1,
                "conclusion": "failure",
                "display_title": "Factory 7 · auto · fixed-model entry",
                "html_url": "https://github.com/acme/widgets/actions/runs/1",
                "updated_at": "2026-11-24T12:00:00Z",
            },
            {
                "id": 2,
                "conclusion": "failure",
                "display_title": "Factory 7 · auto · fixed-model entry",
                "html_url": "https://github.com/acme/widgets/actions/runs/2",
                "updated_at": "2026-11-24T11:00:00Z",
            },
            {"id": 3, "conclusion": "success", "display_title": "Factory 8 · auto"},
        ]

    def repo_api(self, route: str) -> Any:
        self.routes.append(route)
        if route.startswith("actions/workflows/"):
            return {"workflow_runs": self.runs}
        if route.startswith("check-runs/"):
            job = int(route.split("/")[1])
            message = REVIEW_ERROR if job == 11 else "Process completed with exit code 1."
            return [{"annotation_level": "failure", "message": message}]
        run = int(route.split("/")[2])
        return {
            "jobs": [
                {
                    "id": run * 10 + 1,
                    "name": "rotisserie / run / factory",
                    "conclusion": "failure",
                    "steps": [
                        {"name": "Set up job", "conclusion": "success"},
                        {"name": PICK, "conclusion": "failure"},
                    ],
                },
                {
                    "id": run * 10 + 2,
                    "name": f"rotisserie / {PERSIST}",
                    "conclusion": "success" if run == 1 else "failure",
                    "steps": [],
                },
            ]
        }

    def text(self, route: str) -> str:
        job = int(route.split("/")[2])
        if job == 12:
            return (
                f"2026-11-24T12:00:01Z [factory-outcome] no_capacity: No capacity: {REVIEW_ERROR}\n"
                '2026-11-24T12:00:01Z [factory-outcome-class] {"class":"no_capacity",'
                '"source":"nvidia","model":"","target":"pr:5671"}\n'
            )
        return (
            "2026-11-24T11:00:01Z [factory-outcome] failed: No verified saved code or PR "
            "delivery; worker failed: control_plane_failure\n"
        )


def test_failed_runs_carry_step_error_class_provider_target_and_link() -> None:
    hub = Hub()
    github = cast(GitHub, hub)
    start = datetime(2026, 11, 24, 2, 0, tzinfo=UTC)
    found = failures.worker_failures(github, start, tracker=Tracker(github))
    assert "created=%3E%3D2026-11-24T02:00:00Z" in hub.routes[0]
    assert [row.run_id for row in found] == [1, 2]
    first, second = found
    assert (first.job, first.step) == ("run / factory", PICK)
    assert first.error == REVIEW_ERROR
    assert first.outcome_class == "no_capacity"
    assert first.target == "pr:5671"
    assert first.url == "https://github.com/acme/widgets/actions/runs/1"
    assert first.provider
    assert second.outcome_class == "control_plane_failure"
    assert second.target == ""
    groups = failures.group(found)
    assert len(groups) == 1
    assert groups[0].to_dict()["classes"] == {"no_capacity": 1, "control_plane_failure": 1}


def test_first_failed_step_prefers_the_worker_job() -> None:
    jobs: list[Any] = [
        {"name": PERSIST, "steps": [{"name": "Classify", "conclusion": "failure"}]},
        {"name": "run / factory", "steps": [{"name": PICK, "conclusion": "failure"}]},
    ]
    assert failures.first_failed_step(jobs) == ("run / factory", PICK)
    assert failures.first_failed_step([{"name": "x", "conclusion": "timed_out"}]) == ("x", "")


def _cli(argv: list[str], monkeypatch: pytest.MonkeyPatch) -> tuple[int, str]:
    from rotisserie import cli
    from rotisserie.factory_console import commands
    from rotisserie.factory_console.tracker import Clock

    hub = Hub()
    monkeypatch.setattr(GitHub, "repo_api", lambda _self, route: hub.repo_api(route))
    monkeypatch.setattr(GitHub, "text", lambda _self, route: hub.text(route))
    out = io.StringIO()
    code = commands.main(
        cli.parser().parse_args(["factory", "--repo", REPO, *argv]),
        env={"TERM": "dumb"},
        stdout=out,
        stdin=io.StringIO(),
        clock=Clock(now=lambda: datetime(2026, 11, 24, 13, 0, tzinfo=UTC), sleep=lambda _: None),
        executable="sh",
    )
    return code, out.getvalue()


def test_failures_command_prints_groups_and_run_links(monkeypatch: pytest.MonkeyPatch) -> None:
    code, text = _cli(["failures", "--since", "10h"], monkeypatch)
    assert code == 0
    assert f"2×  run / factory > {PICK}" in text
    assert f"error 1×  {REVIEW_ERROR[:160]}" in text
    assert "recorded as: 1 no capacity, 1 factory setup problem" in text
    assert "pr:5671  no_capacity  https://github.com/acme/widgets/actions/runs/1" in text


def test_failures_command_json(monkeypatch: pytest.MonkeyPatch) -> None:
    code, text = _cli(["--json", "failures"], monkeypatch)
    data = json.loads(text.strip().splitlines()[-1])
    assert code == 0
    assert data["count"] == 2
    assert data["groups"][0]["step"] == PICK
    assert data["groups"][0]["runs"][0]["target"] == "pr:5671"


@pytest.mark.parametrize(
    ("query", "expected"),
    [({}, ["failures", "--since", "10h"]), ({"since": "2d"}, ["failures", "--since", "2d"])],
)
def test_web_ui_may_read_failures(query: dict[str, str], expected: list[str]) -> None:
    assert web.read_command("failures", query) == expected


@pytest.mark.parametrize("query", [{"since": "10h;rm"}, {"limit": "9"}])
def test_web_ui_failures_refuses_other_arguments(query: dict[str, str]) -> None:
    with pytest.raises(web.InvalidRequest):
        web.read_command("failures", query)


def test_rows_serialize_for_json_output() -> None:
    hub = Hub()
    rows = failures.worker_failures(
        cast(GitHub, hub), datetime(2026, 11, 24, tzinfo=UTC), tracker=Tracker(cast(GitHub, hub))
    )
    text = json.dumps([group.to_dict() for group in failures.group(rows)])
    assert "pr:5671" in text


@pytest.mark.parametrize(
    "error",
    [
        "Exact requested model could not be selected; no provider fallback",
        "Every available model authored this PR; the review stays queued for another model",
    ],
)
def test_capacity_refusals_recorded_as_setup_problems_count_as_no_capacity(error: str) -> None:
    row = failures.WorkerFailure(
        1, "u", "2026-11-24T18:55:00Z", "7", "run / factory", PICK, error,
        "control_plane_failure", "NVIDIA", "", "pr:5671",
    )  # fmt: skip
    entry = failures.group([row])[0]
    assert entry.to_dict()["classes"] == {"control_plane_failure": 1}
    assert entry.to_dict()["causes"] == {"no_capacity": 1}
