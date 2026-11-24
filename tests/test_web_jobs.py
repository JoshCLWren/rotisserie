"""Background factory jobs for the local web UI, and the static script they need."""

from __future__ import annotations

import http.client
import json
import sys
import threading
import time
from collections.abc import Iterator
from http.server import ThreadingHTTPServer
from importlib.resources import files
from typing import Any

import pytest

from rotisserie import web, web_jobs

FAKE_CLI = """
import json, sys, time
command, rest = sys.argv[2], sys.argv[3:]
if command == "unstick":
    for text in ("Reading open work", "Plan: 2 workers to start"):
        print(json.dumps({"step": text, "phase": "plan"}), file=sys.stderr, flush=True)
        time.sleep(0.05)
    print("a stray warning", file=sys.stderr, flush=True)
    print(json.dumps({"result": "submitted", "started": [{"target": "pr:4"}]}))
elif command == "slow":
    print(json.dumps({"step": "Waiting"}), file=sys.stderr, flush=True)
    time.sleep(float(rest[0]))
    print(json.dumps({"result": "completed"}))
elif command == "broken":
    print("Traceback: boom", file=sys.stderr)
    sys.exit(1)
elif command == "nothing":
    print(json.dumps({"result": "no_work"}))
    sys.exit(3)
else:
    print(json.dumps({"result": "completed", "args": rest}))
"""


def fake_command(repository: str, args: list[str]) -> list[str]:
    return [sys.executable, "-c", FAKE_CLI, repository, *args]


def wait(jobs: web_jobs.Jobs, job: web_jobs.Job, limit: float = 10) -> dict[str, Any]:
    deadline = time.monotonic() + limit
    while job.state == "running" and time.monotonic() < deadline:
        time.sleep(0.02)
    return jobs.view(job)


def test_a_job_reports_its_steps_in_order_then_the_final_json() -> None:
    jobs = web_jobs.Jobs("acme/widgets", command=fake_command)
    job = jobs.start(["unstick", "--yes"], mutating=True)
    started = jobs.view(job)
    assert started["state"] == "running" and started["command"] == "unstick"
    view = wait(jobs, job)
    assert view["state"] == "finished"
    assert [step["step"] for step in view["steps"]] == [
        "Reading open work",
        "Plan: 2 workers to start",
    ]
    assert all(step["at"] >= 0 for step in view["steps"])
    assert view["result"] == {
        "result": "submitted",
        "started": [{"target": "pr:4"}],
        "exit_code": 0,
    }
    assert view["elapsed"] >= 0.1


def test_a_command_without_progress_still_finishes() -> None:
    jobs = web_jobs.Jobs("acme/widgets", command=fake_command)
    view = wait(jobs, jobs.start(["status"], mutating=False))
    assert view["state"] == "finished"
    assert view["steps"] == []
    assert view["result"]["args"] == []


def test_a_nonzero_exit_with_json_is_a_finished_job_the_ui_can_render() -> None:
    jobs = web_jobs.Jobs("acme/widgets", command=fake_command)
    view = wait(jobs, jobs.start(["nothing"], mutating=True))
    assert view["state"] == "finished"
    assert view["result"] == {"result": "no_work", "exit_code": 3}


def test_a_command_without_its_json_contract_fails_with_its_stderr() -> None:
    jobs = web_jobs.Jobs("acme/widgets", command=fake_command)
    view = wait(jobs, jobs.start(["broken"], mutating=True))
    assert view["state"] == "failed"
    assert view["result"]["exit_code"] == 1
    assert "did not return its JSON contract" in view["result"]["error"]
    assert "Traceback: boom" in view["result"]["error"]


def test_a_command_that_cannot_start_fails() -> None:
    jobs = web_jobs.Jobs("acme/widgets", command=lambda repo, args: ["/nonexistent/rotisserie"])
    view = wait(jobs, jobs.start(["status"], mutating=False))
    assert view["state"] == "failed"
    assert view["result"]["error"] == "Could not start Rotisserie."


def test_a_slow_write_is_never_killed_but_a_slow_read_is() -> None:
    jobs = web_jobs.Jobs("acme/widgets", command=fake_command, read_timeout=0.3)
    write = jobs.start(["slow", "1"], mutating=True)
    read = jobs.start(["slow", "1.01"], mutating=False)
    write_view, read_view = wait(jobs, write), wait(jobs, read)
    assert write_view["state"] == "finished"
    assert write_view["result"]["result"] == "completed"
    assert write_view["elapsed"] >= 1
    assert read_view["state"] == "failed"
    assert read_view["result"]["result"] == "timeout"


def test_one_write_at_a_time_and_a_repeated_request_joins_the_running_job() -> None:
    jobs = web_jobs.Jobs("acme/widgets", command=fake_command)
    first = jobs.start(["slow", "0.5"], mutating=True)
    assert jobs.start(["slow", "0.5"], mutating=True) is first
    with pytest.raises(web_jobs.JobRefused) as refused:
        jobs.start(["sync", "pr:4", "--yes"], mutating=True)
    assert refused.value.running == first.id
    wait(jobs, first)
    assert wait(jobs, jobs.start(["sync", "pr:4", "--yes"], mutating=True))["state"] == "finished"


def test_only_a_bounded_number_of_finished_jobs_are_kept() -> None:
    jobs = web_jobs.Jobs("acme/widgets", command=fake_command, keep=2)
    finished = [jobs.start(["status", str(index)], mutating=False) for index in range(4)]
    for job in finished:
        wait(jobs, job)
    kept = [job for job in finished if jobs.get(job.id) is not None]
    assert len(kept) == 2


@pytest.fixture
def server() -> Iterator[tuple[int, list[list[str]]]]:
    calls: list[list[str]] = []

    def command(repository: str, args: list[str]) -> list[str]:
        calls.append(args)
        return fake_command(repository, args)

    jobs = web_jobs.Jobs("acme/widgets", command=command)
    handler = web.make_handler("acme/widgets", "secret-session", jobs=jobs)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd.server_port, calls
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


def get(port: int, path: str, headers: dict[str, str]) -> tuple[int, str, str]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    connection.request("GET", path, headers={"Host": f"127.0.0.1:{port}", **headers})
    response = connection.getresponse()
    body = response.read().decode()
    connection.close()
    return response.status, response.getheader("Content-Type") or "", body


def test_the_script_is_served_with_the_page_checks_and_a_script_type(
    server: tuple[int, list[list[str]]],
) -> None:
    port, _ = server
    status, kind, body = get(port, "/web_ui.js", {})
    assert status == 200
    assert kind == "text/javascript; charset=utf-8"
    assert body.startswith('"use strict";')
    assert "secret-session" not in body
    assert get(port, "/web_ui.js", {"Host": "evil.test"})[0] == 403
    assert get(port, "/web_ui.js", {"Origin": "https://evil.test"})[0] == 403
    _, _, page = get(port, "/", {})
    assert '<script src="/web_ui.js"></script>' in page


def test_the_script_is_package_data_next_to_the_page() -> None:
    assert files("rotisserie").joinpath("web_ui.js").is_file()


def test_a_background_read_returns_a_job_that_needs_the_session(
    server: tuple[int, list[list[str]]],
) -> None:
    port, calls = server
    auth = {"X-Rotisserie-Token": "secret-session"}
    assert get(port, "/api/unstick?background=1", {})[0] == 403
    status, _, body = get(port, "/api/unstick?background=1", auth)
    assert status == 202
    job = json.loads(body)["job"]
    assert calls == [["unstick", "--dry-run"]]
    assert get(port, f"/api/jobs/{job}", {})[0] == 403
    assert get(port, f"/api/jobs/{job}", {**auth, "Origin": "https://evil.test"})[0] == 403
    assert get(port, "/api/jobs/unknown", auth)[0] == 404
    assert get(port, "/api/jobs/../status", auth)[0] == 400
    view: dict[str, Any] = {"state": "running"}
    deadline = time.monotonic() + 10
    while view["state"] == "running" and time.monotonic() < deadline:
        view = json.loads(get(port, f"/api/jobs/{job}", auth)[2])
    assert view["state"] == "finished"
    assert [step["step"] for step in view["steps"]][-1] == "Plan: 2 workers to start"
