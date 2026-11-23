"""Safety and parity checks for the hosted edition of the real control room."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from rotisserie.public_web import render_page, sanitized_snapshot

TEMPLATE = (Path(__file__).resolve().parents[1] / "src/rotisserie/web_ui.html").read_text(
    encoding="utf-8"
)


def old_dashboard(data: dict[str, object]) -> str:
    return (
        '<!doctype html><script id="fleet-snapshot" type="application/json">'
        + json.dumps(data)
        + "</script>"
    )


def snapshot() -> dict[str, object]:
    return {
        "generated_at": "2026-11-23T14:00:00Z",
        "open_prs": 6,
        "open_issues": 42,
        "cooling_slots": 5,
        "unavailable_slots": 2,
        "throughput": {"opened_day": 3, "merged_day": 7, "net_day": -4},
        "workers": [
            {
                "worker": "111",
                "display_name": "Test worker",
                "runtime_source": "example",
                "runtime_model": "example/free",
                "activity": "working",
                "updated": "2026-11-23T14:00:00Z",
                "work": ["pr:123"],
                "heartbeat": {"run": "456", "SECRET_TOKEN": "never-expose"},
                "SECRET_TOKEN": "never-expose",
            }
        ],
        "candidate_health": {
            "models": [
                {
                    "source": "example",
                    "model": "example/free",
                    "status": "healthy",
                    "api_key": "never-expose",
                }
            ]
        },
        "private_metadata": "never-expose",
    }


def test_hosted_ui_is_the_real_control_room_with_a_static_transport() -> None:
    page, data_json = render_page(old_dashboard(snapshot()), TEMPLATE)
    assert "Rotisserie Control Room" in page
    assert 'id="launcher"' in page
    assert 'id="health"' in page
    assert "window.rotisserieHosted.request(path,body)" in page
    assert "window.rotisserieHosted.dispatch(body)" in page
    assert "rotisserie-admin-command.yml" in page
    assert "Full fleet telemetry" in page
    assert "Hosted public snapshot" in page
    assert "__SESSION_TOKEN__" not in page
    assert "__REPOSITORY__" not in page
    assert "never-expose" not in data_json
    assert "never-expose" not in page


def test_public_payload_is_allowlisted_even_when_source_expands() -> None:
    data = sanitized_snapshot(old_dashboard(snapshot()))
    assert data["open_prs"] == 6
    assert data["throughput"]["merged_day"] == 7
    assert data["workers"][0]["work_ref"] == "pr:123"
    assert data["workers"][0]["run"] == "456"
    assert data["candidate_models"] == [
        {"source": "example", "model": "example/free", "status": "healthy"}
    ]
    assert "private_metadata" not in data
    assert "never-expose" not in json.dumps(data)


@pytest.mark.parametrize(
    "source",
    [
        "<html>no fleet snapshot</html>",
        old_dashboard({"generated_at": ""}),
        old_dashboard({"generated_at": "2026-11-23T14:00:00Z", "workers": ["bad"]}),
    ],
)
def test_malformed_source_does_not_expose_untrusted_content(source: str) -> None:
    if "bad" in source:
        assert sanitized_snapshot(source)["workers"] == []
    else:
        with pytest.raises(ValueError):
            sanitized_snapshot(source)


def test_changed_bootstrap_contract_fails_closed() -> None:
    with pytest.raises(ValueError, match="bootstrap"):
        render_page(old_dashboard(snapshot()), TEMPLATE.replace("__SESSION_TOKEN__", "disabled"))
