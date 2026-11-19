"""Tests for the release-writer CLI validation and publishing logic."""

import io
import json
import os
from collections.abc import Callable
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from scripts import release_writer


def _capture_stderr(
    func: Callable[..., object], *args: object, **kwargs: object
) -> str:
    """Capture stderr from a function call that exits unsuccessfully.

    Args:
        func: The function to call.
        *args: Positional arguments to pass to func.
        **kwargs: Keyword arguments to pass to func.

    Returns:
        The captured stderr output as a string.

    """
    f = io.StringIO()
    with redirect_stderr(f):
        try:
            func(*args, **kwargs)
        except SystemExit as exc:
            if exc.code != 2:
                pytest.fail(f"Expected SystemExit with exit code 2, got {exc.code}")
        else:
            pytest.fail("Expected SystemExit with exit code 2")
    return f.getvalue()


def _capture_stdout(
    func: Callable[..., object], *args: object, **kwargs: object
) -> str:
    """Capture stdout from a function call.

    Args:
        func: The function to call.
        *args: Positional arguments to pass to func.
        **kwargs: Keyword arguments to pass to func.

    Returns:
        The captured stdout output as a string.

    """
    f = io.StringIO()
    with redirect_stdout(f):
        func(*args, **kwargs)
    return f.getvalue()


class TestReleaseWriterValidation:
    """Test release-writer input validation."""

    def test_publish_requires_valid_json(self) -> None:
        """Invalid JSON should be rejected.

        Args:
            None.

        Returns:
            None.
        """
        stderr = _capture_stderr(release_writer._validate_release, "not valid json")
        assert "invalid release JSON" in stderr

    def test_publish_requires_object(self) -> None:
        """Payload must be a JSON object.

        Args:
            None.

        Returns:
            None.
        """
        stderr = _capture_stderr(release_writer._validate_release, '"just a string"')
        assert "release payload must be an object" in stderr

    def test_publish_requires_all_fields(self) -> None:
        """All required fields must be present.

        Args:
            None.

        Returns:
            None.
        """
        payload = {"source_repository": "test/repo"}
        stderr = _capture_stderr(release_writer._validate_release, json.dumps(payload))
        assert "missing release fields" in stderr

    def test_publish_rejects_unsupported_fields(self) -> None:
        """Unknown fields should be rejected.

        Args:
            None.

        Returns:
            None.
        """
        payload = _valid_payload()
        payload["unknown_field"] = "value"
        stderr = _capture_stderr(release_writer._validate_release, json.dumps(payload))
        assert "unsupported fields" in stderr

    def test_publish_validates_source_repository(self) -> None:
        """source_repository must be 1-255 characters.

        Args:
            None.

        Returns:
            None.
        """
        payload = _valid_payload()
        payload["source_repository"] = ""
        stderr = _capture_stderr(release_writer._validate_release, json.dumps(payload))
        assert "source_repository must be 1..255 characters" in stderr

        payload["source_repository"] = "a" * 256
        stderr = _capture_stderr(release_writer._validate_release, json.dumps(payload))
        assert "source_repository must be 1..255 characters" in stderr

    def test_publish_validates_source_pr_number(self) -> None:
        """source_pr_number must be positive integer.

        Args:
            None.

        Returns:
            None.
        """
        payload = _valid_payload()
        payload["source_pr_number"] = 0
        stderr = _capture_stderr(release_writer._validate_release, json.dumps(payload))
        assert "source_pr_number must be a positive integer" in stderr

        payload["source_pr_number"] = -1
        stderr = _capture_stderr(release_writer._validate_release, json.dumps(payload))
        assert "source_pr_number must be a positive integer" in stderr

        payload["source_pr_number"] = "not a number"
        stderr = _capture_stderr(release_writer._validate_release, json.dumps(payload))
        assert "source_pr_number must be a positive integer" in stderr

        payload["source_pr_number"] = True
        stderr = _capture_stderr(release_writer._validate_release, json.dumps(payload))
        assert "source_pr_number must be a positive integer" in stderr

        payload["source_pr_number"] = False
        stderr = _capture_stderr(release_writer._validate_release, json.dumps(payload))
        assert "source_pr_number must be a positive integer" in stderr

    def test_publish_validates_source_merge_sha(self) -> None:
        """source_merge_sha must be 7-64 characters.

        Args:
            None.

        Returns:
            None.
        """
        payload = _valid_payload()
        payload["source_merge_sha"] = "short"
        stderr = _capture_stderr(release_writer._validate_release, json.dumps(payload))
        assert "source_merge_sha must be 7..64 characters" in stderr

        payload["source_merge_sha"] = "a" * 65
        stderr = _capture_stderr(release_writer._validate_release, json.dumps(payload))
        assert "source_merge_sha must be 7..64 characters" in stderr

    def test_publish_validates_timestamps(self) -> None:
        """Timestamps must be valid ISO-8601.

        Args:
            None.

        Returns:
            None.
        """
        payload = _valid_payload()
        payload["merged_at"] = "not a timestamp"
        stderr = _capture_stderr(release_writer._validate_release, json.dumps(payload))
        assert "merged_at must be a valid ISO-8601 timestamp" in stderr

        payload["merged_at"] = "2024-01-01T00:00:00Z"
        payload["released_at"] = "invalid"
        stderr = _capture_stderr(release_writer._validate_release, json.dumps(payload))
        assert "released_at must be a valid ISO-8601 timestamp" in stderr

    def test_publish_validates_string_fields(self) -> None:
        """String fields must be non-empty and within length limits.

        Args:
            None.

        Returns:
            None.
        """
        payload = _valid_payload()
        payload["category"] = ""
        stderr = _capture_stderr(release_writer._validate_release, json.dumps(payload))
        assert "category must be non-empty" in stderr

        payload["category"] = "a" * 101
        stderr = _capture_stderr(release_writer._validate_release, json.dumps(payload))
        assert "category must be non-empty and at most 100 characters" in stderr

        payload["category"] = "Valid"
        payload["title"] = ""
        stderr = _capture_stderr(release_writer._validate_release, json.dumps(payload))
        assert "title must be non-empty" in stderr

        payload["title"] = "Valid"
        payload["summary"] = ""
        stderr = _capture_stderr(release_writer._validate_release, json.dumps(payload))
        assert "summary must be non-empty" in stderr

        payload["summary"] = "a" * 1201
        stderr = _capture_stderr(release_writer._validate_release, json.dumps(payload))
        assert "summary must be non-empty and at most 1200 characters" in stderr

    def test_publish_validates_body(self) -> None:
        """Body must be null or <= 6000 characters.

        Args:
            None.

        Returns:
            None.
        """
        payload = _valid_payload()
        payload["body"] = "a" * 6001
        stderr = _capture_stderr(release_writer._validate_release, json.dumps(payload))
        assert "body must be null or at most 6000 characters" in stderr

    def test_publish_validates_visibility(self) -> None:
        """Visibility must be public or internal.

        Args:
            None.

        Returns:
            None.
        """
        payload = _valid_payload()
        payload["visibility"] = "private"
        stderr = _capture_stderr(release_writer._validate_release, json.dumps(payload))
        assert "unsupported visibility" in stderr

    def test_publish_validates_status(self) -> None:
        """Status must be draft, published, or retracted.

        Args:
            None.

        Returns:
            None.
        """
        payload = _valid_payload()
        payload["status"] = "archived"
        stderr = _capture_stderr(release_writer._validate_release, json.dumps(payload))
        assert "unsupported status" in stderr

    def test_publish_validates_provenance_json(self) -> None:
        """provenance_json must be an object.

        Args:
            None.

        Returns:
            None.
        """
        payload = _valid_payload()
        payload["provenance_json"] = "not an object"
        stderr = _capture_stderr(release_writer._validate_release, json.dumps(payload))
        assert "provenance_json must be an object" in stderr

    def test_publish_accepts_valid_payload(self) -> None:
        """A fully valid payload should pass validation and return normalized payload.

        Args:
            None.

        Returns:
            None.
        """
        payload = _valid_payload()
        result = release_writer._validate_release(json.dumps(payload))
        assert result["source_repository"] == "JoshCLWren/comic-pile"
        assert result["source_pr_number"] == 123
        assert result["source_merge_sha"] == "abcdef1234567890"
        assert result["visibility"] == "public"
        assert result["status"] == "published"
        assert result["sort_order"] == 0
        assert result["provenance_json"] == _public_provenance()

    def test_publish_decodes_escaped_apostrophes(self) -> None:
        """Literal apostrophe escapes never reach the stored release copy.

        Args:
            None.

        Returns:
            None.
        """
        payload = _valid_payload()
        payload["title"] = "GitHub links added to What\\u0027s New page"
        payload["summary"] = "The Roll page\\'s rating view now remembers your place."
        payload["body"] = "Reader copy with What\\u2019s inside."

        result = release_writer._validate_release(json.dumps(payload))

        assert result["title"] == "GitHub links added to What's New page"
        assert result["summary"] == "The Roll page's rating view now remembers your place."
        assert result["body"] == "Reader copy with What\u2019s inside."
        assert release_writer._decode_release_escapes("What\\\\u0027s New page") == (
            "What's New page"
        )

    def test_publish_preserves_legitimate_backslashes(self) -> None:
        """Decoding apostrophe escapes must not strip ordinary backslashes.

        Args:
            None.

        Returns:
            None.
        """
        payload = _valid_payload()
        payload["title"] = "Read notes in C:\\Users\\reader"
        payload["summary"] = "Match \\d+ rows in your reading log."
        payload["body"] = "Keep the \\n sequence literal."

        result = release_writer._validate_release(json.dumps(payload))

        assert result["title"] == "Read notes in C:\\Users\\reader"
        assert result["summary"] == "Match \\d+ rows in your reading log."
        assert result["body"] == "Keep the \\n sequence literal."

    def test_publish_sets_defaults(self) -> None:
        """Optional non-evidence fields should get default values.

        Public classification evidence is not optional, so it stays in the
        payload while the remaining optional fields are exercised.

        Args:
            None.

        Returns:
            None.
        """
        payload = _valid_payload()
        # Remove optional fields; classification evidence is not optional
        for field in ("body", "visibility", "status", "sort_order"):
            del payload[field]

        result = release_writer._validate_release(json.dumps(payload))
        assert result["visibility"] == "public"
        assert result["status"] == "published"
        assert result["sort_order"] == 0
        assert result["provenance_json"] == _public_provenance()

    def test_publish_requires_public_classification_evidence(self) -> None:
        """A public payload without classification evidence must be rejected.

        Args:
            None.

        Returns:
            None.
        """
        payload = _valid_payload()
        del payload["provenance_json"]
        stderr = _capture_stderr(release_writer._validate_release, json.dumps(payload))
        assert 'provenance_json.classification must be "public"' in stderr


class TestReleaseWriterCheck:
    """Test the check command for reconciliation."""

    def test_check_validates_pr_number(self) -> None:
        """PR number must be an integer.

        Args:
            None.

        Returns:
            None.
        """
        with patch.object(release_writer, "_request") as mock_request:
            mock_request.return_value = {"exists": False, "release": None}
            stderr = _capture_stderr(
                release_writer._check, "repo", "not-a-number", "abcdef1234567890"
            )
            assert "PR number must be an integer" in stderr
            mock_request.assert_not_called()

    def test_check_calls_api(self) -> None:
        """Check should call the reconciliation endpoint.

        Args:
            None.

        Returns:
            None.
        """
        with patch.object(release_writer, "_request") as mock_request:
            mock_request.return_value = {"exists": True, "release": {"id": 42}}
            with patch.object(
                release_writer, "_api_base", return_value="http://test/api"
            ):
                with patch.object(
                    release_writer, "_token", return_value="test-token"
                ):
                    release_writer._check(
                        "JoshCLWren/comic-pile", "123", "abcdef1234567890"
                    )
            mock_request.assert_called_once()
            args, kwargs = mock_request.call_args
            assert args[0] == "GET"
            assert "source_repository=JoshCLWren%2Fcomic-pile" in args[1]
            assert "source_pr_number=123" in args[1]
            assert "source_merge_sha=abcdef1234567890" in args[1]


class TestReleaseWriterSkip:
    """Test the skip command for internal changes."""

    def test_skip_requires_fields(self) -> None:
        """Skip payload needs required fields.

        Args:
            None.

        Returns:
            None.
        """
        payload = {"source_repository": "test/repo"}
        stderr = _capture_stderr(release_writer._skip, json.dumps(payload))
        assert "skip payload is missing required fields" in stderr

    def test_skip_validates_timestamp(self) -> None:
        """Skip requires valid merged_at timestamp.

        Args:
            None.

        Returns:
            None.
        """
        payload = {
            "source_repository": "test/repo",
            "source_pr_number": 1,
            "source_merge_sha": "abcdef1234567890",
            "merged_at": "invalid",
            "reason": "test",
        }
        stderr = _capture_stderr(release_writer._skip, json.dumps(payload))
        assert "merged_at must be a valid ISO-8601 timestamp" in stderr

    def test_skip_validates_reason(self) -> None:
        """Skip requires non-empty reason <= 500 chars.

        Args:
            None.

        Returns:
            None.
        """
        payload = {
            "source_repository": "test/repo",
            "source_pr_number": 1,
            "source_merge_sha": "abcdef1234567890",
            "merged_at": "2024-01-01T00:00:00Z",
            "reason": "",
        }
        stderr = _capture_stderr(release_writer._skip, json.dumps(payload))
        assert "skip reason must be non-empty" in stderr

        payload["reason"] = "a" * 501
        stderr = _capture_stderr(release_writer._skip, json.dumps(payload))
        assert "skip reason must be non-empty and at most 500 characters" in stderr

    def test_skip_outputs_classification(self) -> None:
        """Skip should output machine-readable classification.

        Args:
            None.

        Returns:
            None.
        """
        payload = {
            "source_repository": "test/repo",
            "source_pr_number": 1,
            "source_merge_sha": "abcdef1234567890",
            "merged_at": "2024-01-01T00:00:00Z",
            "reason": "Internal maintenance only",
        }
        with patch.object(release_writer, "_request") as mock_request:
            mock_request.return_value = {"id": 1}
            with patch.object(
                release_writer, "_api_base", return_value="http://test/api"
            ):
                with patch.object(
                    release_writer, "_token", return_value="test-token"
                ):
                    output = _capture_stdout(
                        release_writer._skip, json.dumps(payload)
                    )
        result = json.loads(output.strip())
        assert result["classification"] == "internal"
        assert result["skipped"] is True
        assert result["recorded"] is True
        assert result["release"] == {"id": 1}
        args = mock_request.call_args[0]
        assert args[0] == "PUT"
        assert args[1] == "http://test/api/"
        published = args[2]
        assert published["source_pr_number"] == 1
        assert published["visibility"] == "internal"
        assert published["summary"] == "Internal maintenance only"
        assert published["provenance_json"] == {
            "classification": "internal",
            "reason": "Internal maintenance only",
            "reader_reachable": False,
            "user_visible_evidence": "",
            "inspected_issue_numbers": [],
            "scope_fences": [],
            "contradicting_evidence": [],
        }


class TestReleaseWriterRequest:
    """Test the _request function."""

    def test_request_builds_correct_headers(self) -> None:
        """Request should include the auth token in headers.

        Args:
            None.

        Returns:
            None.
        """
        with patch("scripts.release_writer.urllib.request.urlopen") as mock_urlopen:
            mock_response = Mock()
            mock_response.read.return_value = json.dumps({"id": 1}).encode()
            mock_response.__enter__ = Mock(return_value=mock_response)
            mock_response.__exit__ = Mock(return_value=False)
            mock_urlopen.return_value = mock_response

            with patch.object(
                release_writer, "_api_base", return_value="http://test/api"
            ):
                with patch.object(
                    release_writer, "_token", return_value="secret-token"
                ):
                    release_writer._request("PUT", "http://test/api/", {"test": "data"})

            call_args = mock_urlopen.call_args
            request = call_args[0][0]
            assert request.get_method() == "PUT"
            assert request.get_header("X-release-writer-token") == "secret-token"
            assert request.get_header("Content-type") == "application/json"
            assert "secret-token" not in request.full_url
            assert "secret-token" not in request.data.decode()

    def test_request_handles_http_error(self) -> None:
        """HTTP errors should be caught and formatted.

        Args:
            None.

        Returns:
            None.
        """
        import urllib.error

        with patch("scripts.release_writer.urllib.request.urlopen") as mock_urlopen:
            http_error = urllib.error.HTTPError(
                url="http://test/api/",
                code=409,
                msg="Conflict",
                hdrs={},
                fp=io.BytesIO(b'{"detail": "Source conflict"}'),
            )
            mock_urlopen.side_effect = http_error

            with patch.object(
                release_writer, "_api_base", return_value="http://test/api"
            ):
                with patch.object(
                    release_writer, "_token", return_value="secret-token"
                ):
                    stderr = _capture_stderr(
                        release_writer._request,
                        "PUT",
                        "http://test/api/",
                        {"test": "data"},
                    )
            assert "release API returned HTTP 409" in stderr

    def test_request_handles_url_error(self) -> None:
        """URL errors (connection refused, etc.) should be caught.

        Args:
            None.

        Returns:
            None.
        """
        import urllib.error

        with patch("scripts.release_writer.urllib.request.urlopen") as mock_urlopen:
            url_error = urllib.error.URLError(reason="Connection refused")
            mock_urlopen.side_effect = url_error

            with patch.object(
                release_writer, "_api_base", return_value="http://test/api"
            ):
                with patch.object(
                    release_writer, "_token", return_value="secret-token"
                ):
                    stderr = _capture_stderr(
                        release_writer._request,
                        "PUT",
                        "http://test/api/",
                        {"test": "data"},
                    )
            assert "release API request failed: Connection refused" in stderr


class TestReleaseWriterEnvironment:
    """Test environment variable handling."""

    def test_missing_api_url(self) -> None:
        """Missing RELEASE_API_URL should fail.

        Args:
            None.

        Returns:
            None.
        """
        with patch.dict(
            os.environ,
            {"RELEASE_API_URL": "", "RELEASE_WRITER_TOKEN": "token"},
            clear=True,
        ):
            stderr = _capture_stderr(release_writer._api_base)
            assert "RELEASE_API_URL is required" in stderr

    def test_missing_token(self) -> None:
        """Missing RELEASE_WRITER_TOKEN should fail.

        Args:
            None.

        Returns:
            None.
        """
        with patch.dict(
            os.environ,
            {"RELEASE_API_URL": "http://test/api", "RELEASE_WRITER_TOKEN": ""},
            clear=True,
        ):
            stderr = _capture_stderr(release_writer._token)
            assert "RELEASE_WRITER_TOKEN is required" in stderr


def _provenance(result: dict[str, object]) -> dict[str, object]:
    """Return the provenance object of one validated release payload.

    Args:
        result: Validated release payload.

    Returns:
        The payload's provenance object.
    """
    provenance = result["provenance_json"]
    assert isinstance(provenance, dict)
    return provenance


def _public_provenance() -> dict[str, object]:
    """Return complete reader-visible classification evidence for a public release.

    Returns:
        Provenance object satisfying every public classification requirement.
    """
    return {
        "classification": "public",
        "classification_reason": "Queue page behavior changed for every reader.",
        "user_visible_evidence": (
            "frontend/src/pages/QueuePage.tsx now shows a saved-search filter."
        ),
        "reader_reachable": True,
        "inspected_issue_numbers": [1070],
        "scope_fences": [],
        "contradicting_evidence": [],
        "reader_reachable_path": "Queue page at /queue",
    }


def _valid_payload() -> dict[str, object]:
    """Return a valid release payload for testing."""
    return {
        "source_repository": "JoshCLWren/comic-pile",
        "source_pr_number": 123,
        "source_merge_sha": "abcdef1234567890",
        "merged_at": "2024-01-01T00:00:00Z",
        "released_at": "2024-01-01T00:00:00Z",
        "category": "What's New",
        "title": "Test Release",
        "summary": "A test release summary",
        "body": "More details here",
        "visibility": "public",
        "status": "published",
        "sort_order": 0,
        "provenance_json": _public_provenance(),
    }


class TestReleaseWriterMeaningfulContent:
    """Test public release copy cannot be placeholder-sized."""

    def test_publish_rejects_placeholder_public_title_and_summary(self) -> None:
        """One-character public titles and summaries are never valid release notes.

        Args:
            None.

        Returns:
            None.
        """
        payload = _valid_payload()
        payload["title"] = "T"
        payload["summary"] = "S"
        stderr = _capture_stderr(release_writer._validate_release, json.dumps(payload))
        assert "title must contain meaningful release content" in stderr

    def test_publish_rejects_short_public_summary(self) -> None:
        """Public summaries must carry meaningful visible content.

        Args:
            None.

        Returns:
            None.
        """
        payload = _valid_payload()
        payload["summary"] = "Sh"
        stderr = _capture_stderr(release_writer._validate_release, json.dumps(payload))
        assert "summary must contain meaningful release content" in stderr

    def test_publish_rejects_short_public_category(self) -> None:
        """Public categories must be at least two visible characters.

        Args:
            None.

        Returns:
            None.
        """
        payload = _valid_payload()
        payload["category"] = "B"
        stderr = _capture_stderr(release_writer._validate_release, json.dumps(payload))
        assert "category must contain meaningful release content" in stderr

    def test_publish_measures_visible_text_after_stripping_markdown(self) -> None:
        """Markdown link syntax must not inflate the meaningful-content length.

        Args:
            None.

        Returns:
            None.
        """
        payload = _valid_payload()
        payload["title"] = "[Queue search](https://github.com/JoshCLWren/comic-pile/pull/123)"
        result = release_writer._validate_release(json.dumps(payload))
        assert result["title"] == (
            "[Queue search](https://github.com/JoshCLWren/comic-pile/pull/123)"
        )

    def test_publish_allows_short_internal_records(self) -> None:
        """Internal skip records are exempt from public copy quality checks.

        Args:
            None.

        Returns:
            None.
        """
        payload = _valid_payload()
        payload["visibility"] = "internal"
        payload["title"] = "T"
        payload["summary"] = "S"
        result = release_writer._validate_release(json.dumps(payload))
        assert result["title"] == "T"
        assert result["summary"] == "S"


class TestReleaseWriterReaderFacingCopy:
    """Test public release copy cannot carry internal engineering artifacts."""

    def test_publish_rejects_internal_ticket_references(self) -> None:
        """GitHub ticket numbers must never ship to the reader-facing feed.

        Args:
            None.

        Returns:
            None.
        """
        payload = _valid_payload()
        payload["title"] = "incomplete #1551 fix"
        stderr = _capture_stderr(release_writer._validate_release, json.dumps(payload))
        assert "title must use reader-facing product language" in stderr
        assert "#1551" in stderr

    def test_publish_rejects_schema_identifiers(self) -> None:
        """Database and schema identifiers must never ship to the reader-facing feed.

        Args:
            None.

        Returns:
            None.
        """
        payload = _valid_payload()
        payload["summary"] = "Added source_roll_event_id instrumentation detail."
        stderr = _capture_stderr(release_writer._validate_release, json.dumps(payload))
        assert "summary must use reader-facing product language" in stderr
        assert "source_roll_event_id" in stderr

    def test_publish_rejects_phase_terminology(self) -> None:
        """Implementation phase terminology must never ship to readers.

        Args:
            None.

        Returns:
            None.
        """
        payload = _valid_payload()
        payload["title"] = "Roll history loads instantly (Phase 2 and 3)"
        stderr = _capture_stderr(release_writer._validate_release, json.dumps(payload))
        assert "title must use reader-facing product language" in stderr
        assert "Phase 2" in stderr

    def test_publish_rejects_unfinished_work_commentary(self) -> None:
        """Unfinished-work commentary must never ship to readers.

        Args:
            None.

        Returns:
            None.
        """
        payload = _valid_payload()
        payload["summary"] = "This update is still incomplete but shipped anyway."
        stderr = _capture_stderr(release_writer._validate_release, json.dumps(payload))
        assert "summary must use reader-facing product language" in stderr
        assert "incomplete" in stderr

    def test_publish_rejects_known_misspellings(self) -> None:
        """Known misspellings are rejected so entries are spell-checked before publication.

        Args:
            None.

        Returns:
            None.
        """
        payload = _valid_payload()
        payload["title"] = "Smoother appearnence for saved piles"
        stderr = _capture_stderr(release_writer._validate_release, json.dumps(payload))
        assert "title must be spell-checked before publication" in stderr
        assert "appearnence" in stderr
        assert "appearance" in stderr

    def test_publish_ignores_artifacts_hidden_behind_markdown_links(self) -> None:
        """Visible text is inspected after Markdown link resolution.

        A link whose visible text is a bare ticket reference must be rejected even
        though the raw string hides it behind URL syntax.

        Args:
            None.

        Returns:
            None.
        """
        payload = _valid_payload()
        payload["title"] = (
            "[#1551](https://github.com/JoshCLWren/comic-pile/pull/1551) roll fix"
        )
        stderr = _capture_stderr(release_writer._validate_release, json.dumps(payload))
        assert "title must use reader-facing product language" in stderr

    def test_publish_allows_engineering_detail_in_body_and_internal_records(self) -> None:
        """The reader-facing gate governs public published copy only.

        Engineering detail stays allowed in the non-rendered body and in internal
        visibility records used for durable skip bookkeeping.

        Args:
            None.

        Returns:
            None.
        """
        payload = _valid_payload()
        payload["body"] = "Engineering follow-up tracked in issue #1551 (source_roll_event_id)."
        result = release_writer._validate_release(json.dumps(payload))
        body = result["body"]
        assert isinstance(body, str)
        assert "issue #1551" in body

        internal = _valid_payload()
        internal["visibility"] = "internal"
        internal["title"] = "Internal change (PR #1240)"
        internal["summary"] = "Tracks source_roll_event_id backfill for Phase 2."
        internal_result = release_writer._validate_release(json.dumps(internal))
        assert internal_result["visibility"] == "internal"

    def test_skip_helper_keeps_internal_record_exemption(self) -> None:
        """Internal skip records with engineering language still validate.

        Args:
            None.

        Returns:
            None.
        """
        payload = {
            "source_repository": "JoshCLWren/comic-pile",
            "source_pr_number": 1241,
            "source_merge_sha": "abcdef1234567890",
            "merged_at": "2024-01-01T00:00:00Z",
            "reason": "Test-only refactor tracked by wip cleanup work.",
        }
        result = release_writer._validate_release(
            json.dumps(
                {
                    "source_repository": payload["source_repository"],
                    "source_pr_number": payload["source_pr_number"],
                    "source_merge_sha": payload["source_merge_sha"],
                    "merged_at": payload["merged_at"],
                    "released_at": payload["merged_at"],
                    "category": "Internal",
                    "title": f"Internal change (PR #{payload['source_pr_number']})",
                    "summary": payload["reason"],
                    "visibility": "internal",
                    "status": "published",
                    "sort_order": 0,
                    "provenance_json": {"classification": "internal"},
                }
            )
        )
        assert result["visibility"] == "internal"


class TestReleaseWriterRetract:
    """Test the retract command for removing broken public releases."""

    def test_retract_validates_pr_number(self) -> None:
        """PR number must be an integer.

        Args:
            None.

        Returns:
            None.
        """
        with patch.object(release_writer, "_request") as mock_request:
            stderr = _capture_stderr(
                release_writer._retract, "repo", "not-a-number", "abcdef1234567890"
            )
            assert "PR number must be an integer" in stderr
            mock_request.assert_not_called()

    def test_retract_resolves_source_then_retracts(self, capsys) -> None:
        """Retract resolves the source identity and posts to the retract endpoint.

        Args:
            capsys: Pytest output capture helper.

        Returns:
            None.
        """
        with patch.object(release_writer, "_request") as mock_request:
            mock_request.side_effect = [
                {"exists": True, "release": {"id": 42}},
                {"id": 42, "status": "retracted"},
            ]
            with patch.object(
                release_writer, "_api_base", return_value="http://test/api"
            ):
                with patch.object(
                    release_writer, "_token", return_value="test-token"
                ):
                    release_writer._retract(
                        "JoshCLWren/comic-pile", "123", "abcdef1234567890"
                    )
            assert mock_request.call_count == 2
            source_args = mock_request.call_args_list[0][0]
            assert source_args[0] == "GET"
            assert "source_repository=JoshCLWren%2Fcomic-pile" in source_args[1]
            assert "source_pr_number=123" in source_args[1]
            assert "source_merge_sha=abcdef1234567890" in source_args[1]
            retract_args = mock_request.call_args_list[1][0]
            assert retract_args[0] == "POST"
            assert retract_args[1] == "http://test/api/42/retract"
        output = capsys.readouterr().out
        result = json.loads(output.strip())
        assert result["retracted"] is True
        assert result["release"]["id"] == 42

    def test_retract_fails_when_source_is_missing(self) -> None:
        """Retracting an unknown source identity must not invent a record.

        Args:
            None.

        Returns:
            None.
        """
        with patch.object(release_writer, "_request") as mock_request:
            mock_request.return_value = {"exists": False, "release": None}
            with patch.object(
                release_writer, "_api_base", return_value="http://test/api"
            ):
                with patch.object(
                    release_writer, "_token", return_value="test-token"
                ):
                    stderr = _capture_stderr(
                        release_writer._retract,
                        "JoshCLWren/comic-pile",
                        "123",
                        "abcdef1234567890",
                    )
            assert "no release ledger record exists for this source identity" in stderr


class TestReleaseWriterPublicClassificationEvidence:
    """Public publication requires auditable reader-visible classification evidence."""

    def test_publish_rejects_weak_user_visible_evidence(self) -> None:
        """Placeholder evidence must not justify a public release.

        Args:
            None.

        Returns:
            None.
        """
        payload = _valid_payload()
        payload["provenance_json"]["user_visible_evidence"] = "looks good"
        stderr = _capture_stderr(release_writer._validate_release, json.dumps(payload))
        assert "provenance_json.user_visible_evidence must describe the change" in stderr

    def test_publish_rejects_public_without_reader_reachability(self) -> None:
        """A change with no shipped reader path must be classified internal.

        Args:
            None.

        Returns:
            None.
        """
        payload = _valid_payload()
        payload["provenance_json"]["reader_reachable"] = False
        stderr = _capture_stderr(release_writer._validate_release, json.dumps(payload))
        assert "provenance_json.reader_reachable must be true" in stderr

    def test_publish_rejects_public_when_scope_fence_has_no_contradiction(self) -> None:
        """A declared scope fence blocks publication until it is contradicted.

        Args:
            None.

        Returns:
            None.
        """
        payload = _valid_payload()
        payload["provenance_json"]["scope_fences"] = ["no frontend callers"]
        stderr = _capture_stderr(release_writer._validate_release, json.dumps(payload))
        assert "linked issue scope fences contradict a public release" in stderr
        assert "no frontend callers" in stderr

    def test_publish_accepts_public_when_scope_fence_is_contradicted(self) -> None:
        """Documented contradicting evidence is the audited escape hatch.

        Args:
            None.

        Returns:
            None.
        """
        payload = _valid_payload()
        payload["provenance_json"]["scope_fences"] = ["no frontend callers"]
        payload["provenance_json"]["contradicting_evidence"] = [
            "The shipped queue page already calls this endpoint."
        ]
        result = release_writer._validate_release(json.dumps(payload))
        assert result["visibility"] == "public"

    def test_publish_rejects_capability_claim_without_reachable_path(self) -> None:
        """'You can now' claims must name the shipped reader path.

        Args:
            None.

        Returns:
            None.
        """
        payload = _valid_payload()
        payload["title"] = "Saved queue filters"
        payload["summary"] = "You can now save and reuse queue filters."
        provenance = payload["provenance_json"]
        assert isinstance(provenance, dict)
        del provenance["reader_reachable_path"]
        stderr = _capture_stderr(release_writer._validate_release, json.dumps(payload))
        assert "provenance_json.reader_reachable_path must be a string" in stderr

    def test_publish_rejects_mistyped_inspected_issue_numbers(self) -> None:
        """Inspected issue provenance must be a list of issue numbers.

        Args:
            None.

        Returns:
            None.
        """
        payload = _valid_payload()
        payload["provenance_json"]["inspected_issue_numbers"] = ["1070"]
        stderr = _capture_stderr(release_writer._validate_release, json.dumps(payload))
        assert "provenance_json.inspected_issue_numbers must be a list of issue numbers" in stderr

    def test_publish_rejects_oversized_evidence(self) -> None:
        """Classification evidence must stay bounded.

        Args:
            None.

        Returns:
            None.
        """
        payload = _valid_payload()
        payload["provenance_json"]["classification_reason"] = "r" * 1001
        stderr = _capture_stderr(release_writer._validate_release, json.dumps(payload))
        assert "provenance_json.classification_reason must be at most 1000 characters" in stderr

    def test_internal_records_skip_public_evidence_requirements(self) -> None:
        """Internal records remain publishable without reader-visible evidence.

        Args:
            None.

        Returns:
            None.
        """
        payload = _valid_payload()
        payload["visibility"] = "internal"
        payload["provenance_json"] = {
            "classification": "internal",
            "reader_reachable": False,
        }
        result = release_writer._validate_release(json.dumps(payload))
        assert result["visibility"] == "internal"


class TestReleaseWriterSkipEvidence:
    """Internal skips record auditable classification provenance."""

    def test_skip_records_inspected_issues_and_scope_fences(self) -> None:
        """Skip evidence is durable and auditable in the ledger.

        Args:
            None.

        Returns:
            None.
        """
        payload = {
            "source_repository": "test/repo",
            "source_pr_number": 2910,
            "source_merge_sha": "abcdef1234567890",
            "merged_at": "2024-01-01T00:00:00Z",
            "reason": "Backend CBL adoption with the reader UI deferred.",
            "inspected_issue_numbers": [2128],
            "scope_fences": ["no frontend callers"],
        }
        with patch.object(release_writer, "_request") as mock_request:
            mock_request.return_value = {"id": 1}
            with patch.object(release_writer, "_api_base", return_value="http://test/api"):
                with patch.object(release_writer, "_token", return_value="test-token"):
                    _capture_stdout(release_writer._skip, json.dumps(payload))
        published = mock_request.call_args[0][2]
        assert published["provenance_json"]["inspected_issue_numbers"] == [2128]
        assert published["provenance_json"]["scope_fences"] == ["no frontend callers"]
        assert published["provenance_json"]["reader_reachable"] is False

    def test_skip_rejects_reader_visible_evidence(self) -> None:
        """An internal skip must not also claim reader-visible change.

        Args:
            None.

        Returns:
            None.
        """
        payload = {
            "source_repository": "test/repo",
            "source_pr_number": 1,
            "source_merge_sha": "abcdef1234567890",
            "merged_at": "2024-01-01T00:00:00Z",
            "reason": "Internal maintenance only",
            "user_visible_evidence": "Readers can now see the new board view.",
        }
        stderr = _capture_stderr(release_writer._skip, json.dumps(payload))
        assert "user_visible_evidence must be empty for an internal record" in stderr

    def test_skip_rejects_unsupported_fields(self) -> None:
        """Unknown skip fields must fail instead of being silently dropped.

        Args:
            None.

        Returns:
            None.
        """
        payload = {
            "source_repository": "test/repo",
            "source_pr_number": 1,
            "source_merge_sha": "abcdef1234567890",
            "merged_at": "2024-01-01T00:00:00Z",
            "reason": "Internal maintenance only",
            "visibility": "public",
        }
        stderr = _capture_stderr(release_writer._skip, json.dumps(payload))
        assert "skip payload contains unsupported fields" in stderr


class TestReleaseWriterScopeFenceDetection:
    """Deterministic scope-fence detection over real issue failure shapes."""

    def test_detects_cbl_deferred_ui_fence(self) -> None:
        """The #2910 CBL shape must be detected as decisive.

        Uses the real scope-boundary wording from issue 2127.

        Args:
            None.

        Returns:
            None.
        """
        text = (
            "## Scope boundaries\n\n"
            "- No final browser/review UI. #2128 owns that.\n"
            "- Do not add standalone readiness/preflight UI.\n"
        )
        assert release_writer._scope_fences(text) == ["no shipped ui", "ui not shipped"]

    def test_detects_roll_bootstrap_fence(self) -> None:
        """The #2916 Roll bootstrap shape must be detected as decisive.

        Uses the real acceptance wording from issue 2717.

        Args:
            None.

        Returns:
            None.
        """
        text = (
            "- [ ] No frontend caller changes in this issue.\n\n"
            "## Scope fence\n\n"
            "Do not migrate the idle UI, add Map series, or change Roll eligibility.\n"
        )
        assert release_writer._scope_fences(text) == ["no frontend callers", "scope fence"]

    def test_detects_refactor_and_ambiguous_fences(self) -> None:
        """Internal refactors and partial rollouts must be detected.

        Args:
            None.

        Returns:
            None.
        """
        text = (
            "Internal refactor: React Query component split with behavior preserved. "
            "Staged rollout for readers."
        )
        assert release_writer._scope_fences(text) == [
            "behavior preserved",
            "internal refactor",
            "staged rollout",
        ]

    def test_ignores_public_issue_prose(self) -> None:
        """Reader-visible issue prose must not fabricate fences.

        Args:
            None.

        Returns:
            None.
        """
        text = "The queue page now offers saved filters and a faster roll."
        assert release_writer._scope_fences(text) == []


class TestReleaseWriterPublishGrounding:
    """Publish-time grounding compares the copy with merged-diff and issue evidence."""

    def test_backend_only_capability_claim_is_rejected(self) -> None:
        """#2910/#2916 shape: capability claim with no reader-facing diff.

        Args:
            None.

        Returns:
            None.
        """
        payload = _valid_payload()
        payload["source_pr_number"] = 2910
        payload["title"] = "Comic Book Library reading plans"
        payload["summary"] = "You can now build a reading plan from library lists."
        provenance = payload["provenance_json"]
        assert isinstance(provenance, dict)
        provenance.pop("reader_reachable_path")

        def fake_read(url: str):
            if "/files" in url:
                return [{"filename": "app/services/queue.py", "status": "modified"}]
            return {"number": 2910, "title": "CBL adoption", "body": "Closes #2128."}

        with patch.object(release_writer, "_github_read", side_effect=fake_read):
            stderr = _capture_stderr(
                release_writer._validate_publish_grounding, payload
            )
        assert "no reader-facing product surface" in stderr

    def test_reader_visible_diff_allows_capability_claim(self) -> None:
        """A real reader-facing change keeps its public capability claim.

        Args:
            None.

        Returns:
            None.
        """
        payload = _valid_payload()
        payload["title"] = "Saved queue filters"
        payload["summary"] = "You can now save and reuse queue filters."

        def fake_read(url: str):
            return [{"filename": "frontend/src/pages/QueuePage.tsx"}]

        with patch.object(release_writer, "_github_read", side_effect=fake_read):
            release_writer._validate_publish_grounding(payload)

    def test_static_shell_change_allows_capability_claim(self) -> None:
        """A shipped-shell style or markup change reaches the reader.

        Args:
            None.

        Returns:
            None.
        """
        payload = _valid_payload()
        payload["title"] = "Faster queue rendering"
        payload["summary"] = "You can now see the queue render without a delay."

        def fake_read(url: str):
            return [{"filename": "static/css/styles.css"}]

        with patch.object(release_writer, "_github_read", side_effect=fake_read):
            release_writer._validate_publish_grounding(payload)

    def test_linked_issue_fence_blocks_undocumented_publication(self) -> None:
        """A fence in linked issue context blocks publication.

        Args:
            None.

        Returns:
            None.
        """
        payload = _valid_payload()
        payload["provenance_json"]["scope_fences"] = []

        def fake_read(url: str):
            if "/files" in url:
                return [{"filename": "frontend/src/pages/QueuePage.tsx"}]
            if "/issues/" in url:
                return {
                    "number": 2128,
                    "title": "CBL support",
                    "body": "No frontend caller changes and the UI is out of scope.",
                }
            return {"number": 2910, "title": "CBL adoption", "body": "Closes #2128."}

        with patch.object(release_writer, "_github_read", side_effect=fake_read):
            stderr = _capture_stderr(
                release_writer._validate_publish_grounding, payload
            )
        assert "linked issue context contains decisive scope fences" in stderr

    def test_contradicting_evidence_skips_grounding_checks(self) -> None:
        """Documented contradicting evidence is the audited escape hatch.

        Args:
            None.

        Returns:
            None.
        """
        payload = _valid_payload()
        payload["provenance_json"]["contradicting_evidence"] = [
            "The shipped queue page already calls this endpoint today."
        ]

        def unexpected_read(url: str):
            raise AssertionError(f"grounding must short-circuit, got {url}")

        with patch.object(release_writer, "_github_read", side_effect=unexpected_read):
            release_writer._validate_publish_grounding(payload)

    def test_unavailable_github_does_not_block_publication(self) -> None:
        """Grounding fails open on unreadable evidence and closed on real evidence.

        Args:
            None.

        Returns:
            None.
        """
        payload = _valid_payload()
        payload["title"] = "Saved queue filters"
        payload["summary"] = "You can now save and reuse queue filters."

        with patch.object(release_writer, "_github_read", return_value=None):
            release_writer._validate_publish_grounding(payload)

    def test_internal_public_grounding_is_skipped(self) -> None:
        """Internal records never run publish-time grounding.

        Args:
            None.

        Returns:
            None.
        """
        payload = _valid_payload()
        payload["visibility"] = "internal"
        payload["provenance_json"] = {"classification": "internal"}

        def unexpected_read(url: str):
            raise AssertionError(f"grounding must skip internal records, got {url}")

        with patch.object(release_writer, "_github_read", side_effect=unexpected_read):
            release_writer._validate_publish_grounding(payload)


class TestReleaseWriterEngineeringClassification:
    """Regression tests for deterministic internal classification and scope fences."""

    def test_reading_session_note_stays_public(self) -> None:
        """A reader-facing reading-session note is never auto-classified internal.

        Reading sessions are a reader-facing feature, so the word "session" is
        not engineering jargon and must not override recorded reader-facing
        evidence.

        Args:
            None.

        Returns:
            None.
        """
        payload = _valid_payload()
        payload["summary"] = "Restore your reading session after an update."
        result = release_writer._validate_release(json.dumps(payload))
        assert result["visibility"] == "public"

    def test_reading_session_title_is_not_a_scope_fence(self) -> None:
        """A pull-request title about reading sessions never fences publication.

        Args:
            None.

        Returns:
            None.
        """
        assert (
            release_writer._scope_fences(
                "History: session detail renders a bare number instead of the die ladder"
            )
            == []
        )

    def test_ordinary_product_words_are_not_scope_fences(self) -> None:
        """Ordinary reader language never creates an engineering scope fence.

        Args:
            None.

        Returns:
            None.
        """
        text = (
            "Readers can audit their reading history, check the reliability of "
            "their queue, and see proof that a session was saved."
        )
        assert release_writer._scope_fences(text) == []

    def test_factory_automation_wording_classifies_copy_not_linked_context(
        self,
    ) -> None:
        """Factory automation wording classifies served copy, never blocks a note.

        A pull-request title that mentions internal factory work describes how
        the change was built, but only copy that would actually reach readers
        is classified against the engineering-term table, so a clean
        reader-facing note for that change keeps publishing.

        Args:
            None.

        Returns:
            None.
        """
        decisive, _suggestive = release_writer._engineering_terms(
            "Improve factory session reliability."
        )
        assert decisive == ["factory automation"]
        assert release_writer._scope_fences("The factory is quiet today.") == []
        assert release_writer._scope_fences("Improve factory session reliability.") == []

    def test_suggestive_term_keeps_recorded_reader_facing_evidence_public(self) -> None:
        """A suggestive term never overrides recorded reader-facing evidence.

        Args:
            None.

        Returns:
            None.
        """
        payload = _valid_payload()
        payload["summary"] = "Improved reliability of the reading history page."
        result = release_writer._validate_release(json.dumps(payload))
        assert result["visibility"] == "public"

    def test_suggestive_term_without_evidence_classifies_internal(self) -> None:
        """A suggestive term classifies internal when no reader evidence exists.

        Args:
            None.

        Returns:
            None.
        """
        payload = _valid_payload()
        payload["summary"] = "Improved reliability of the internal queue worker."
        payload["provenance_json"] = {}
        result = release_writer._validate_release(json.dumps(payload))
        provenance = _provenance(result)
        assert result["visibility"] == "internal"
        assert provenance["reader_reachable"] is False
        assert provenance["user_visible_evidence"] == ""

    def test_endpoint_literal_classifies_release_internal(self) -> None:
        """Release copy that quotes an endpoint literal is internal.

        Args:
            None.

        Returns:
            None.
        """
        payload = _valid_payload()
        payload["summary"] = "Added POST /api/v1/threads/1:setCurrentIssue for clients."
        result = release_writer._validate_release(json.dumps(payload))
        terms = _provenance(result)["internal_engineering_terms"]
        assert isinstance(terms, list)
        assert result["visibility"] == "internal"
        assert "endpoint literal" in terms

    def test_decisive_artifact_overrides_reader_facing_evidence(self) -> None:
        """A decisive artifact classifies internal even with reader evidence.

        Reader-facing copy cannot quote an endpoint literal, so recorded
        reader-facing evidence cannot justify publishing it.

        Args:
            None.

        Returns:
            None.
        """
        payload = _valid_payload()
        payload["summary"] = "Added POST /api/v1/threads/1:setCurrentIssue for clients."
        result = release_writer._validate_release(json.dumps(payload))
        assert result["visibility"] == "internal"
        assert _provenance(result)["classification"] == "internal"

    def test_diff_fragment_classifies_release_internal(self) -> None:
        """Release copy that quotes a diff fragment is internal.

        Args:
            None.

        Returns:
            None.
        """
        payload = _valid_payload()
        payload["summary"] = "Factory review controller accepts diff hunk evidence."
        payload["body"] = "diff --git a/scripts/release_writer.py b/scripts/release_writer.py"
        result = release_writer._validate_release(json.dumps(payload))
        terms = _provenance(result)["internal_engineering_terms"]
        assert isinstance(terms, list)
        assert result["visibility"] == "internal"
        assert "diff fragment" in terms

    def test_diff_frame_at_summary_start_classifies_internal(self) -> None:
        """A diff frame at the start of the summary cannot escape classification.

        The copy fields are joined with newlines, so a fragment at the start
        of a field is still a line-start match; a space-joined scan would move
        it behind the preceding field and miss it.

        Args:
            None.

        Returns:
            None.
        """
        payload = _valid_payload()
        payload["summary"] = "+++ b/frontend/src/pages/RollPage.tsx"
        result = release_writer._validate_release(json.dumps(payload))
        terms = _provenance(result)["internal_engineering_terms"]
        assert isinstance(terms, list)
        assert result["visibility"] == "internal"
        assert "diff fragment" in terms

    def test_diff_frame_at_body_start_classifies_internal(self) -> None:
        """A diff header at the start of the body cannot escape classification.

        Args:
            None.

        Returns:
            None.
        """
        payload = _valid_payload()
        payload["body"] = "--- a/frontend/src/services/api.ts"
        result = release_writer._validate_release(json.dumps(payload))
        terms = _provenance(result)["internal_engineering_terms"]
        assert isinstance(terms, list)
        assert result["visibility"] == "internal"
        assert "diff fragment" in terms

    def test_internal_classification_keeps_audit_provenance(self) -> None:
        """Classifying a release internal never wipes the recorded audit trail.

        Args:
            None.

        Returns:
            None.
        """
        payload = _valid_payload()
        payload["summary"] = "Improve factory session reliability."
        payload["provenance_json"] = {
            "classification": "public",
            "classification_reason": "Session page behavior changed.",
            "user_visible_evidence": "frontend/src/pages/SessionPage.tsx now reloads.",
            "reader_reachable": True,
            "inspected_issue_numbers": [3649],
            "scope_fences": ["no shipped ui"],
            "contradicting_evidence": ["Session page ships today."],
        }
        result = release_writer._validate_release(json.dumps(payload))
        provenance = _provenance(result)
        assert result["visibility"] == "internal"
        assert provenance["inspected_issue_numbers"] == [3649]
        assert provenance["scope_fences"] == ["no shipped ui"]
        assert provenance["contradicting_evidence"] == ["Session page ships today."]

    def test_worker_boilerplate_does_not_create_scope_fence(self) -> None:
        """The factory attribution footer must not fence a product pull request.

        Args:
            None.

        Returns:
            None.
        """
        repository = "JoshCLWren/comic-pile"
        number = 100

        def fake_read(url: str):
            if f"/pulls/{number}" in url:
                return {
                    "number": number,
                    "title": "Saved queue filters",
                    "body": (
                        "Add saved queue filters.\n\n"
                        "Model: glm-4.5-flash\n"
                        "Source: z-ai\n"
                        "Worker: opencode-free-model-factory-94\n\n"
                        "Produced by fixed-model Factory 94 (Z.AI GLM 4.5 Flash). "
                        "Normal ComicPile exact-head factory merge gates apply."
                    ),
                }
            return None

        with patch.object(release_writer, "_github_read", side_effect=fake_read):
            fences = release_writer._linked_scope_fences(repository, number)
        assert fences == []

    def test_durable_worker_identifier_does_not_create_scope_fence(self) -> None:
        """A durable worker identifier alone never fences a product pull request.

        Args:
            None.

        Returns:
            None.
        """
        assert (
            release_writer._scope_fences(
                release_writer._strip_worker_boilerplate(
                    "Worker: opencode-free-model-factory-94"
                )
            )
            == []
        )

    def test_real_internal_statement_still_fences_with_boilerplate(self) -> None:
        """A genuine internal statement still fences beside worker boilerplate.

        Args:
            None.

        Returns:
            None.
        """
        repository = "JoshCLWren/comic-pile"
        number = 101

        def fake_read(url: str):
            if f"/pulls/{number}" in url:
                return {
                    "number": number,
                    "title": "Internal refactor",
                    "body": (
                        "Internal refactor: behavior preserved.\n\n"
                        "Produced by fixed-model Factory 46 (worker)."
                    ),
                }
            return None

        with patch.object(release_writer, "_github_read", side_effect=fake_read):
            fences = release_writer._linked_scope_fences(repository, number)
        assert "behavior preserved" in fences
        assert "internal refactor" in fences

    def test_linked_context_artifact_mentions_are_not_scope_fences(self) -> None:
        """Engineering artifacts in a description never block a public note.

        Issue and pull-request bodies legitimately quote endpoint literals
        and diff frames (issue #3649 itself lists repro examples such as
        `POST /api/v1/threads`). The decisive table applies only to served
        copy; the linked-context scan fences on statements that the
        reader-visible portion is not in the change, so a reader-facing fix
        linked to an issue full of quoted artifacts still publishes with
        valid public evidence and clean copy.

        Args:
            None.

        Returns:
            None.
        """
        decisive, _suggestive = release_writer._engineering_terms(
            "POST /api/roll now reports the failure to readers."
        )
        assert decisive == ["endpoint literal"]
        assert (
            release_writer._scope_fences(
                "Added POST /api/v1/threads/{}:setCurrentIssue; see diff --git a/x b/x "
                "and the +++ b/utils line for where. No reader-visible behavior changed "
                "in the UI, so the copy stays internal-only."
            )
            == ["internal only"]
        )
        assert (
            release_writer._scope_fences(
                "diff --git a/app/models.py b/app/models.py\n"
                "--- a/app/models.py\n+++ b/app/models.py"
            )
            == []
        )
