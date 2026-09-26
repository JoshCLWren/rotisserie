"""Tests demonstrating pure dependency/executable-work extraction.

Acceptance criteria covered:
- explicit dependencies
- multiple dependencies
- all-resolved eligibility
- unresolved blocking
- casual-reference non-blocking
- no ComicPile-specific exclusions/leaks
"""
from latticery_extraction.dependency_policy import (
    MANUAL_ONLY_MARKER,
    dependency_declarations,
    has_unresolved_dependencies,
    is_explicit_dependency_reference,
    parse_dependency_numbers,
)
from latticery_extraction.executable_policy import eligibility_reason, is_executable


class TestExplicitDependencies:
    def test_single_prerequisite(self) -> None:
        assert parse_dependency_numbers("Depends on #2126") == {2126}

    def test_multiple_comma_separated(self) -> None:
        body = "Depends on #2126, #2127, and #2128 being merged"
        assert parse_dependency_numbers(body) == {2126, 2127, 2128}

    def test_multiple_plus_separated(self) -> None:
        body = "Depends on #2126 + #2127 + #2128 being deployed."
        assert parse_dependency_numbers(body) == {2126, 2127, 2128}

    def test_case_insensitive_declaration(self) -> None:
        assert parse_dependency_numbers("depends on #42") == {42}


class TestCasualReferencesNonBlocking:
    def test_casual_mentions_ignored(self) -> None:
        body = "See also #100 and #200 for context."
        assert parse_dependency_numbers(body) == set()

    def test_casual_after_declaration_not_prerequisite(self) -> None:
        body = "Depends on #2126.\n\nRelated cleanup tracked in #2104 for context."
        assert parse_dependency_numbers(body) == {2126}

    def test_casual_on_same_line_after_prose_ends_cluster(self) -> None:
        body = "Depends on #10 and #11 being merged; see also #12 for audited reads."
        assert parse_dependency_numbers(body) == {10, 11}

    def test_explicit_reference_detected(self) -> None:
        assert is_explicit_dependency_reference("Depends on #99", 99) is True

    def test_casual_reference_not_explicit(self) -> None:
        assert is_explicit_dependency_reference("Mention #99 elsewhere", 99) is False


class TestMultiplePrerequisites:
    def test_all_resolved_is_executable(self) -> None:
        assert is_executable("Depends on #10, #20", {30, 40}) is True

    def test_some_unresolved_blocks(self) -> None:
        assert is_executable("Depends on #10, #20", {10}) is False

    def test_all_unresolved_blocks(self) -> None:
        assert is_executable("Depends on #10, #20", {10, 20}) is False


class TestUnresolvedBlocking:
    def test_unresolved_true(self) -> None:
        assert has_unresolved_dependencies("Depends on #10", {10, 20}) is True

    def test_unresolved_false_when_resolved(self) -> None:
        assert has_unresolved_dependencies("Depends on #10", {20}) is False

    def test_unresolved_false_no_declaration(self) -> None:
        assert has_unresolved_dependencies("Just a normal issue", {10}) is False

    def test_blocking_reason_shows_numbers(self) -> None:
        reason = eligibility_reason("Depends on #77", {77})
        assert reason is not None
        assert "77" in reason


class TestManualOnly:
    def test_manual_only_blocks(self) -> None:
        assert is_executable("Work", {10}, manual_only_true=True) is False

    def test_marker_in_body_blocks(self) -> None:
        assert is_executable(f"{MANUAL_ONLY_MARKER}\nWork", set()) is False

    def test_manual_reason(self) -> None:
        assert eligibility_reason(MANUAL_ONLY_MARKER + "\nX", set()) == "manual-only marker present"


class TestNoHostLeakage:
    """Ensure no ComicPile-specific exclusions or numbers leak."""

    def test_no_hardcoded_issue_numbers_in_domain(self) -> None:
        # The domain modules should not contain specific issue IDs.
        import inspect
        import latticery_extraction.dependency_policy as dp
        import latticery_extraction.executable_policy as ep

        source = inspect.getsource(dp) + inspect.getsource(ep)
        # The known excluded issues from ComicPile factory policy are 679, 1093, 1109.
        for bad in (679, 1093, 1109):
            assert str(bad) not in source, f"host-specific issue number {bad} leaked into domain"

    def test_no_comic_pile_labels(self) -> None:
        import inspect
        import latticery_extraction.dependency_policy as dp

        source = inspect.getsource(dp)
        assert "factory:unowned" not in source
        assert "ralph-status" not in source

    def test_dependency_declarations_structured(self) -> None:
        decls = dependency_declarations("Depends on #1, #2 and #3")
        assert len(decls) == 1
        assert decls[0].references == {1, 2, 3}
        assert decls[0].raw_text.startswith("Depends on")
