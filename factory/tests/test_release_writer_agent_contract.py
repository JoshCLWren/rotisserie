"""Tests for the dedicated release-writer agent read-only safety contract."""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
RELEASE_WRITER_AGENT = ROOT / ".opencode" / "agents" / "release-writer.md"
RELEASE_WRITER_HELPER = ROOT / "scripts" / "release_writer.py"
RELEASE_WRITER_WORKFLOW = ROOT / ".github" / "workflows" / "release-writer.yml"


def _load_agent_permission() -> dict:
    """Load the release-writer agent's permission block from its frontmatter.

    Args:
        None.

    Returns:
        The parsed YAML permission mapping for the release-writer agent.
    """
    text = RELEASE_WRITER_AGENT.read_text(encoding="utf-8")
    assert text.startswith("---"), "release-writer agent must start with YAML frontmatter"
    _, frontmatter, _ = text.split("---", 2)
    config = yaml.safe_load(frontmatter)
    return config["permission"]


def test_release_writer_cannot_edit_or_run_arbitrary_shell_commands() -> None:
    """Require structural denial of edits and arbitrary shell execution."""
    permission = _load_agent_permission()
    assert permission["edit"] == "deny"
    assert permission["task"] == "deny"
    assert permission["external_directory"] == "deny"
    assert permission["question"] == "deny"
    assert permission["bash"]["*"] == "deny"


def test_release_writer_only_calls_validated_helper() -> None:
    """The only allowed command must be the credential-holding helper script."""
    permission = _load_agent_permission()
    allowed_bash = {
        command for command, decision in permission["bash"].items() if decision == "allow"
    }
    assert allowed_bash == {"python scripts/release_writer.py *"}


def test_release_writer_has_no_github_metadata_mutation_command() -> None:
    """Reject merge, label, comment, edit, or raw gh api commands."""
    permission = _load_agent_permission()
    allowed_bash = {
        command for command, decision in permission["bash"].items() if decision == "allow"
    }
    for command in allowed_bash:
        assert "gh api" not in command
        assert "merge" not in command
        assert "label" not in command
        assert "comment" not in command
        assert "git push" not in command


def test_release_writer_helper_documents_all_commands() -> None:
    """Every documented helper command must exist in the release-writer script."""
    helper_text = RELEASE_WRITER_HELPER.read_text(encoding="utf-8")
    agent_text = RELEASE_WRITER_AGENT.read_text(encoding="utf-8")
    for command in ("check", "publish", "skip", "retract", "pr", "files", "issues"):
        assert f'command == "{command}"' in helper_text, f"missing {command} handler"
        assert f"release_writer.py {command}" in agent_text, f"missing {command} docs"


def test_release_writer_contract_requires_reader_facing_copy_rules() -> None:
    """The release-writer instructions must demand reader-facing, spell-checked copy."""
    agent_text = RELEASE_WRITER_AGENT.read_text(encoding="utf-8")
    assert "State the user-visible change and its benefit" in agent_text
    for forbidden_artifact in ("#1551", "source_roll_event_id", "Phase 2 and 3"):
        assert f"Never include {forbidden_artifact}" in agent_text or (
            f"`{forbidden_artifact}`" in agent_text
        ), f"missing guidance about {forbidden_artifact}"
    assert "incomplete fix" in agent_text
    assert "loading states" in agent_text
    assert "Spell-check every title, category, and summary" in agent_text


def test_release_writer_contract_forbids_engineering_artifacts_in_copy() -> None:
    """The agent instructions must forbid engineering artifacts in public copy.

    Returns:
        None.
    """
    agent_text = RELEASE_WRITER_AGENT.read_text(encoding="utf-8")
    assert "Never publish engineering artifacts" in agent_text
    assert "POST /api/v1/threads" in agent_text
    assert "classifies" in agent_text and "as internal" in agent_text


def test_release_writer_contract_documents_exact_source_and_skip_shape() -> None:
    """Avoid wasting model calls on malformed check and skip helper invocations."""
    agent_text = RELEASE_WRITER_AGENT.read_text(encoding="utf-8")
    assert "check <repository> <pr-number> <merge-sha>" in agent_text
    for field in (
        "source_repository",
        "source_pr_number",
        "source_merge_sha",
        "merged_at",
        "reason",
    ):
        assert field in agent_text
    assert "Do not rename them to `repository`, `pr_number`, or `merge_sha`" in agent_text


def test_release_writer_workflow_falls_back_on_provider_rate_limits() -> None:
    """A transient NVIDIA 429 should be handled by the deterministic controller."""
    workflow = RELEASE_WRITER_WORKFLOW.read_text(encoding="utf-8")
    controller = (ROOT / "scripts" / "release_reconcile.py").read_text(encoding="utf-8")
    assert "models<<EOF" in workflow
    assert "RELEASE_WRITER_MODELS" in workflow
    assert "_RATE_LIMIT_PATTERN" in controller
    assert "Too Many Requests" in controller
    assert "rate.?limit" in controller
    assert "trying the next model" in controller


def test_release_writer_all_read_helpers_are_get_only(monkeypatch) -> None:
    """All read-only helper commands (_recent, _pr, _files, _issues) use GET only."""
    import io
    import json as jsonlib

    from scripts import release_writer

    monkeypatch.setenv("GH_TOKEN", "test-token")

    def make_fake():
        # Provide enough responses for any call order
        available = {
            "pulls": jsonlib.dumps(
                [{"number": 1, "merged_at": "2026-01-01T00:00:00Z",
                  "merge_commit_sha": "abc", "title": "PR"}]
            ).encode(),
            "pull": jsonlib.dumps(
                {"number": 1082, "title": "PR #1082", "body": "Closes #1070.",
                 "merged": True, "merged_at": "2026-08-11T00:00:00Z",
                 "merge_commit_sha": "abc123", "state": "closed",
                 "html_url": "https://example.com", "user": {"login": "t"}}
            ).encode(),
            "files": jsonlib.dumps(
                [{"filename": "x.py", "status": "modified",
                  "additions": 1, "deletions": 0}]
            ).encode(),
            "issues": jsonlib.dumps(
                {"number": 1082, "title": "PR #1082", "body": "Closes #1070."}
            ).encode(),
        }

        def fake_urlopen(request, timeout=20):
            assert request.get_method() == "GET", f"Expected GET, got {request.get_method()}"
            assert getattr(request, "data", None) is None, "GitHub reads must not send a body"
            url = str(getattr(request, "full_url", getattr(request, "url", "unknown")))
            key = "files" if "/files" in url else ("pull" if "/pulls/" in url else ("pulls" if "/pulls?" in url else "issues"))
            body_bytes = available.get(key, available["pull"])
            return io.BytesIO(body_bytes)

        return fake_urlopen

    monkeypatch.setattr(release_writer.urllib.request, "urlopen", make_fake())

    # Exercise all four read-only helpers
    release_writer._recent("JoshCLWren/comic-pile", "1")
    release_writer._pr("JoshCLWren/comic-pile", "1082")
    release_writer._files("JoshCLWren/comic-pile", "1082")
    release_writer._issues("JoshCLWren/comic-pile", "1082")


def test_release_writer_contract_requires_public_classification_evidence() -> None:
    """A public note must carry auditable reader-visible classification evidence.

    Returns:
        None.
    """
    agent_text = RELEASE_WRITER_AGENT.read_text(encoding="utf-8")
    for field in (
        "classification",
        "classification_reason",
        "user_visible_evidence",
        "reader_reachable",
        "inspected_issue_numbers",
        "scope_fences",
        "contradicting_evidence",
        "reader_reachable_path",
    ):
        assert f'"{field}"' in agent_text, f"missing public provenance field {field}"
    assert "provenance_json" in agent_text
    assert "classify as `internal`" in agent_text


def test_release_writer_contract_defaults_ambiguous_changes_to_internal() -> None:
    """Ambiguous or incomplete evidence must classify as internal, not public.

    Returns:
        None.
    """
    agent_text = RELEASE_WRITER_AGENT.read_text(encoding="utf-8")
    assert "When the evidence is ambiguous, conflicting, or incomplete" in agent_text
    for internal_category in (
        "backend or API prerequisite",
        "persistence, schema, or data-model groundwork",
        "internal refactor",
        "test, tooling, build, or deployment plumbing",
        "whose supported product UI remains unshipped",
        "explicitly fences the user-facing portion into a follow-up",
    ):
        assert internal_category in agent_text


def test_release_writer_contract_requires_scope_fence_contradiction() -> None:
    """Decisive scope fences must be read before publishing and contradicted.

    Returns:
        None.
    """
    agent_text = RELEASE_WRITER_AGENT.read_text(encoding="utf-8")
    assert "no frontend caller changes" in agent_text
    assert "no final UI" in agent_text
    assert "`contradicting_evidence` is required whenever `scope_fences` is non-empty" in agent_text
    helper_text = RELEASE_WRITER_HELPER.read_text(encoding="utf-8")
    assert "_scope_fences" in helper_text
    assert "_validate_publish_grounding" in helper_text


def test_release_writer_helper_validates_public_evidence_and_grounding() -> None:
    """The credential-holding helper must enforce the classification guardrails.

    Returns:
        None.
    """
    helper_text = RELEASE_WRITER_HELPER.read_text(encoding="utf-8")
    assert "_validate_public_evidence" in helper_text
    assert "_validate_internal_evidence" in helper_text
    assert "_validate_release(sys.argv[2])" in helper_text
    assert "_validate_publish_grounding(payload)" in helper_text
