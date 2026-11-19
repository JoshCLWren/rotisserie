"""Direct-Neon release publication is isolated, validated, and idempotent."""

from __future__ import annotations

import json
from contextlib import AbstractContextManager, nullcontext
from pathlib import Path
from typing import Never, Self

import pytest

from scripts import release_neon_publish as direct
from scripts import release_reconcile, release_writer


def _payload() -> dict[str, object]:
    return {
        "source_repository": "JoshCLWren/comic-pile",
        "source_pr_number": 123,
        "source_merge_sha": "a" * 40,
        "merged_at": "2026-10-20T20:00:00Z",
        "released_at": "2026-10-20T20:00:00Z",
        "category": "Internal",
        "title": "Internal change (PR 123)",
        "summary": "Only internal tooling changed in this release.",
        "body": None,
        "visibility": "internal",
        "status": "published",
        "sort_order": 0,
        "provenance_json": {
            "classification": "internal",
            "reason": "Only internal tooling changed.",
            "reader_reachable": False,
            "user_visible_evidence": "",
            "inspected_issue_numbers": [],
            "scope_fences": [],
            "contradicting_evidence": [],
        },
    }


class FakeCursor:
    """Record the SQL operations executed by the release publisher."""

    def __init__(self, *, selections: list[list[tuple[object, ...]]], inserted: tuple[object, ...] | None = None):
        """Prepare the predefined SELECT and INSERT outcomes."""
        self.selections = list(selections)
        self.inserted = inserted
        self.current: list[tuple[object, ...]] | tuple[object, ...] | None = None
        self.inserts = 0

    def __enter__(self) -> Self:
        """Enter the in-memory cursor context."""
        return self

    def __exit__(self, *_args: object) -> bool:
        """Exit the in-memory cursor context without suppressing errors."""
        return False

    def execute(self, statement: str, _parameters: tuple[object, ...]) -> Self:
        """Simulate SELECT and INSERT statements."""
        if "SELECT id" in statement:
            self.current = self.selections.pop(0)
        elif "INSERT INTO releases" in statement:
            self.inserts += 1
            self.current = self.inserted
        else:
            raise AssertionError(f"unexpected SQL: {statement}")
        return self

    def fetchall(self) -> list[tuple[object, ...]]:
        """Return the rows from the most recent SELECT."""
        assert isinstance(self.current, list)
        return self.current

    def fetchone(self) -> tuple[object, ...] | None:
        """Return the row from the most recent INSERT."""
        assert self.current is None or isinstance(self.current, tuple)
        return self.current


class FakeConnection:
    """Provide a transaction and cursor for in-memory tests."""

    def __init__(self, cursor: FakeCursor) -> None:
        """Attach the fake cursor to this connection."""
        self._cursor = cursor

    def transaction(self) -> AbstractContextManager[object]:
        """Return a transaction context with no external effects."""
        return nullcontext()

    def cursor(self) -> FakeCursor:
        """Return the test cursor."""
        return self._cursor


def test_exact_existing_source_is_idempotent() -> None:
    """Avoid rewriting releases with an identical source identity."""
    payload = _payload()
    cursor = FakeCursor(selections=[[(1, 123, "a" * 40)]])
    assert direct.publish_payload(FakeConnection(cursor), payload) == "already_present"
    assert cursor.inserts == 0


def test_conflicting_merge_sha_cannot_be_overwritten() -> None:
    """Reject an existing release with a different merge SHA."""
    cursor = FakeCursor(selections=[[(1, 123, "b" * 40)]])
    with pytest.raises(direct.ReleaseIdentityConflictError):
        direct.publish_payload(FakeConnection(cursor), _payload())
    assert cursor.inserts == 0


def test_new_release_is_inserted_once() -> None:
    """Insert a previously unknown merged PR exactly once."""
    cursor = FakeCursor(selections=[[]], inserted=(7,))
    assert direct.publish_payload(FakeConnection(cursor), _payload()) == "inserted"
    assert cursor.inserts == 1


def test_conflict_after_concurrent_insert_is_checked() -> None:
    """Recheck identity after a competing writer wins an insert."""
    cursor = FakeCursor(
        selections=[[], [(9, 555, "a" * 40)]],
        inserted=None,
    )
    with pytest.raises(direct.ReleaseIdentityConflictError):
        direct.publish_payload(FakeConnection(cursor), _payload())


def test_staging_never_contacts_vercel_or_neon(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Ensure staging never performs HTTP publication or database writes."""
    candidate = tmp_path / "candidate.json"
    monkeypatch.setenv("RELEASE_WRITER_STAGE_FILE", str(candidate))
    monkeypatch.delenv("RELEASE_API_URL", raising=False)
    monkeypatch.delenv("RELEASE_WRITER_TOKEN", raising=False)

    def no_network(*_args: object, **_kwargs: object) -> Never:
        raise AssertionError("staging must not use HTTP")

    monkeypatch.setattr(release_writer.urllib.request, "urlopen", no_network)
    base = release_writer._api_base()
    source = (
        base + "/source?source_repository=JoshCLWren%2Fcomic-pile"
        + "&source_pr_number=123&source_merge_sha=" + "a" * 40
    )
    assert release_writer._request("GET", source)["exists"] is False
    assert release_writer._request("PUT", base + "/", _payload())["id"] == 0
    assert release_writer._request("GET", source)["exists"] is True
    assert json.loads(candidate.read_text()) == _payload()
    with pytest.raises(SystemExit):
        release_writer._request("POST", base + "/123/retract")


def test_publisher_validates_real_merge_before_database_credentials(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Reject a candidate whose merge SHA differs from verified GitHub data."""
    candidate = tmp_path / "candidate.json"
    candidate.write_text(json.dumps(_payload()), encoding="utf-8")
    monkeypatch.setattr(
        release_reconcile,
        "_fetch_one",
        lambda _repo, _pr: release_reconcile.Source(
            number=123,
            merged_at="2026-10-20T20:00:00Z",
            merge_commit_sha="a" * 40,
            title="PR 123",
        ),
    )
    result = direct._load_validated(candidate, "JoshCLWren/comic-pile", 123)
    assert result["source_pr_number"] == 123
    bad = _payload()
    bad["source_merge_sha"] = "b" * 40
    candidate.write_text(json.dumps(bad), encoding="utf-8")
    with pytest.raises(direct.ReleaseIdentityConflictError):
        direct._load_validated(candidate, "JoshCLWren/comic-pile", 123)


def test_stage_summary_does_not_claim_publication(monkeypatch: pytest.MonkeyPatch) -> None:
    """Distinguish staged candidates from successfully published releases."""
    monkeypatch.setenv("RELEASE_WRITER_STAGE_FILE", "/tmp/stage.json")
    assert release_reconcile._outcome_from_release({"visibility": "public"}) == "staged_public"
    assert release_reconcile._outcome_from_release({"visibility": "internal"}) == "staged_internal"
