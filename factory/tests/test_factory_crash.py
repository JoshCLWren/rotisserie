"""Crash capture: the hook stores a crash and still fails; item failures stay isolated."""

from __future__ import annotations

import importlib
import os
import sqlite3
import subprocess
import sys
import textwrap
import traceback
from pathlib import Path

import pytest

crash = importlib.import_module("factory_crash")
crash_store = importlib.import_module("factory_crash_store")
libsql = importlib.import_module("factory_libsql")

SCRIPTS = Path(crash.__file__).resolve().parent
CRASHING = textwrap.dedent(
    """
    import sys
    sys.path.insert(0, {scripts!r})
    import factory_crash

    def replace_factory_labels(inactive):
        if inactive:
            checked_at = 1
        return checked_at

    def main():
        print("data on stdout")
        return replace_factory_labels(False)

    raise SystemExit(factory_crash.run(main))
    """
)


def _env(tmp_path: Path, store: Path | str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("FACTORY_TURSO", "GITHUB_"))}
    env.update(
        {
            "FACTORY_STATE_SQLITE": str(store),
            "GITHUB_REPOSITORY": "acme/widgets",
            "GITHUB_RUN_ID": "45655094820",
            "GITHUB_WORKFLOW_REF": (
                "acme/widgets/.github/workflows/fixed-model-factory-dispatch.yml@refs/heads/main"
            ),
            "GITHUB_JOB": "dispatch",
            "SOME_API_TOKEN": "s3cr3t-value-123",
        }
    )
    return env


def _run(tmp_path: Path, store: Path | str) -> subprocess.CompletedProcess[str]:
    script = tmp_path / "entry.py"
    script.write_text(CRASHING.format(scripts=str(SCRIPTS)))
    return subprocess.run(
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
        env=_env(tmp_path, store),
        check=False,
        timeout=60,
    )


def _rows(path: Path) -> list[sqlite3.Row]:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        return list(conn.execute("SELECT * FROM factory_crash ORDER BY id"))
    finally:
        conn.close()


def test_an_uncaught_exception_is_stored_annotated_and_still_fails_the_run(
    tmp_path: Path,
) -> None:
    store = tmp_path / "store.db"
    result = _run(tmp_path, store)
    assert result.returncode == 1
    assert result.stdout == "data on stdout\n"
    assert "Traceback (most recent call last)" in result.stderr
    assert "UnboundLocalError: cannot access local variable 'checked_at'" in result.stderr
    assert "::error file=" in result.stderr and "title=Factory crash::" in result.stderr
    assert "UnboundLocalError in entry.py:9 replace_factory_labels" in result.stderr
    (found,) = _rows(store)
    assert found["repo"] == "acme/widgets" and found["kind"] == "python" and found["fatal"] == 1
    assert (found["script"], found["function"], found["line"]) == (
        "entry.py",
        "replace_factory_labels",
        9,
    )
    assert found["workflow"] == "fixed-model-factory-dispatch.yml"
    assert found["run_url"] == "https://github.com/acme/widgets/actions/runs/45655094820"
    assert "checked_at" in found["message"]
    assert "Traceback" in found["traceback"] and "line 13, in main" in found["traceback"]
    second = _run(tmp_path, store)
    assert second.returncode == 1
    first, again = _rows(store)
    assert first["fingerprint"] == again["fingerprint"]


def test_a_store_failure_never_masks_the_original_exception(tmp_path: Path) -> None:
    broken = tmp_path / "not-a-database"
    broken.mkdir()
    result = _run(tmp_path, broken)
    assert result.returncode == 1
    assert "Factory crash not stored" in result.stderr
    assert "UnboundLocalError: cannot access local variable 'checked_at'" in result.stderr
    assert result.stderr.index("UnboundLocalError: cannot") < result.stderr.index("not stored")


def test_without_a_store_the_hook_only_prints(tmp_path: Path) -> None:
    result = _run(tmp_path, "")
    assert result.returncode == 1
    assert "::error file=" in result.stderr and "not stored" not in result.stderr


def _raise_here() -> None:
    raise KeyError("missing")


def _caught() -> BaseException:
    try:
        _raise_here()
    except KeyError as error:
        return error
    raise AssertionError("unreachable")


def test_the_fingerprint_is_stable_and_names_the_innermost_factory_frame() -> None:
    first, second = crash.describe(_caught()), crash.describe(_caught())
    assert first.fingerprint == second.fingerprint and len(first.fingerprint) == 12
    assert (first.function, first.exception_type) == ("_raise_here", "KeyError")
    assert first.fingerprint == crash.fingerprint(
        "python", "KeyError", "/elsewhere/" + first.script, "_raise_here", first.line
    )
    assert crash.fingerprint("python", "KeyError", "a.py", "f", 2) != crash.fingerprint(
        "python", "KeyError", "a.py", "f", 3
    )
    assert crash.fingerprint("item", "KeyError", "a.py", "f", 2) != crash.fingerprint(
        "python", "KeyError", "a.py", "f", 2
    )
    frames = traceback.StackSummary.from_list(
        [
            traceback.FrameSummary(str(SCRIPTS / "factory-work-controller.py"), 236, "outer"),
            traceback.FrameSummary(str(SCRIPTS / "factory_crash.py"), 9, "item"),
            traceback.FrameSummary("/usr/lib/python3/json/decoder.py", 355, "raw_decode"),
        ]
    )
    chosen = crash._factory_frame(frames)
    assert chosen is not None and (chosen.name, chosen.lineno) == ("outer", 236)


def test_scrub_removes_tokens_store_urls_and_secret_values() -> None:
    env = {"FACTORY_TURSO_URL": "libsql://factory-acme.turso.io", "X_API_TOKEN": "abcdefgh1234"}
    text = (
        "gh: ghp_0123456789abcdefghijABCDEFGHIJ failed for libsql://factory-acme.turso.io "
        "with Bearer eyJhbGciOi.eyJzdWIiOiJ4In0.c2lnbmF0dXJl and abcdefgh1234 at "
        "https://user:pass@example.com/x"
    )
    clean = crash.scrub(text, env)
    for secret in ("ghp_0123", "turso.io", "eyJhbGciOi", "abcdefgh1234", "user:pass"):
        assert secret not in clean
    assert len(crash.short_message("x" * 1000, env)) == crash.MESSAGE_LIMIT


def test_a_long_traceback_keeps_its_innermost_frames() -> None:
    lines = [f'  File "outer{i}.py", line {i}, in f{i}' for i in range(2000)]
    text = "Traceback (most recent call last):\n" + "\n".join(lines) + "\nValueError: boom\n"
    capped = crash.cap_traceback(text)
    assert len(capped) <= crash.TRACEBACK_LIMIT + 80
    assert capped.startswith("... ") and capped.endswith("ValueError: boom\n")
    assert "outer1999.py" in capped and "outer0.py" not in capped


def test_an_item_github_failure_is_recorded_and_skipped_but_code_errors_raise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FACTORY_STATE_SQLITE", str(tmp_path / "store.db"))
    monkeypatch.delenv("FACTORY_TURSO_URL", raising=False)
    done: list[int] = []
    for number in (1, 2):
        with crash.item_failure(f"#{number}"):
            if number == 1:
                raise crash.GitHubCallError(
                    "gh api PUT labels failed: unexpected end of JSON input"
                )
            done.append(number)
    assert done == [2]
    (found,) = _rows(tmp_path / "store.db")
    assert found["kind"] == "item" and found["fatal"] == 0
    assert found["exception_type"] == "GitHubCallError"
    skipped = crash.item("#3", lambda: (_ for _ in ()).throw(crash.GitHubCallError("x")))
    assert skipped is crash.SKIPPED and not skipped
    assert crash.item("#4", lambda value: value * 2, 21) == 42
    with pytest.raises(UnboundLocalError), crash.item_failure("#5"):
        raise UnboundLocalError("cannot access local variable 'checked_at'")
    with pytest.raises(KeyError), crash.item_failure("#6"):
        raise KeyError("labels")


def test_the_export_groups_occurrences_newest_first_without_tracebacks(tmp_path: Path) -> None:
    store = libsql.FileStore(tmp_path / "store.db")
    env = {"GITHUB_REPOSITORY": "acme/widgets", "GITHUB_RUN_ID": "7"}
    loud = crash.describe(_caught())
    quiet = crash.shell_crash("worker.sh", 40, "main", "gh pr view", 1)
    for when, found in ((100, loud), (200, loud), (300, quiet), (400, loud)):
        store.batch([*crash._schema(libsql), (crash.INSERT, crash.row(found, env, when))])
    exported = crash_store.export(store, "acme/widgets", hours=1, now=450)
    groups = exported["groups"]
    assert [group["fingerprint"] for group in groups] == [loud.fingerprint, quiet.fingerprint]
    assert groups[0]["count"] == 3 and groups[0]["first_at"] == crash_store.iso(100)
    assert groups[0]["last_at"] == crash_store.iso(400) and len(groups[0]["times"]) == 3
    assert all("traceback" not in group for group in groups)
    newest = crash_store.newest(store, "acme/widgets", loud.fingerprint[:8])
    assert newest is not None and newest["count"] == 3 and "KeyError" in newest["traceback"]
    assert crash_store.prune(store, "acme/widgets", now=250 + 14 * 86400) == 2
    with pytest.raises(ValueError):
        crash_store.newest(store, "acme/widgets", "ab")
