"""Pick, per task, a ranked model the worker will accept, or say why none can take it.

A review never runs on a model the review rule excludes: the merge gate does
not count self-review. The worker skips its own author guard when the model is
pinned, so the planner applies the rule itself. ``review_excluded_models`` is
the only place that states the rule; swap it for the factory's shared rule
function once that lands.
"""

from __future__ import annotations

import json

from rotisserie.factory_console import runtime
from rotisserie.factory_console.github import GitHub, GitHubError

Ranked = list[tuple[str, str, str]]


def review_excluded_models(github: GitHub, pr: int) -> tuple[str, ...]:
    """Models that may not review the PR: every recorded author (the worker's current rule).

    None when the comments are unreadable, as the worker does.
    """
    try:
        pages = json.loads(
            github.gh(
                "api",
                "--paginate",
                "--slurp",
                f"repos/{github.repo}/issues/{pr}/comments?per_page=100",
            )
        )
    except GitHubError, ValueError:
        return ()
    rows = [
        item
        for page in (pages if isinstance(pages, list) else [])
        for item in (page if isinstance(page, list) else [page])
        if isinstance(item, dict)
    ]
    policy = runtime.module("factory_review_policy")
    return tuple(policy.known_author_models(policy.controller_comment_bodies(rows), pr=pr))


def authored(model: str, authors: tuple[str, ...]) -> bool:
    if model == "auto" or not authors:
        return False
    policy = runtime.module("factory_review_policy")
    return any(policy.same_actual_model(model, author) for author in authors)


def pick(
    ranked: Ranked, tried: set[str], authors: tuple[str, ...] = ()
) -> tuple[str, str, str] | str:
    """(provider, model, why) for the best usable model, or why none is usable."""
    wrote = [model for _, model, _ in ranked if authored(model, authors)]
    usable = [entry for entry in ranked if entry[1] not in wrote]
    if not usable:
        return (
            f"every available model authored this PR ({', '.join(wrote)}); "
            "the review waits for a model that did not write it"
        )
    note = f"; skipped {', '.join(wrote)}: authored this PR" if wrote else ""
    for provider, model, why in usable:
        if model not in tried:
            missed = f"; {', '.join(sorted(tried))} just missed on it" if tried else ""
            return provider, model, why + missed + note
    provider, model, why = usable[0]
    return provider, model, why + "; every offered model already missed on it recently" + note
