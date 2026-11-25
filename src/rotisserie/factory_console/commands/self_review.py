from __future__ import annotations

import argparse

from rotisserie.factory_console import runtime
from rotisserie.factory_console.commands._shared import EXIT_OK, Outcome, _split_target
from rotisserie.factory_console.context import Context, UsageError
from rotisserie.factory_console.github import GitHubError, obj, string
from rotisserie.factory_console.service import review
from rotisserie.factory_console.tracker import EXIT_CODES, Result

HELP = """\
Let the model that wrote a pull request's current head review it (owner exception).

  baste allow-self-review pr:N        add factory:allow-self-review as you

The exception covers one pull request and its current head only. The factory
accepts the label only when a person added it, never a bot or GitHub Actions,
so run this with your own GitHub login (the hosted control room cannot grant
it). Every other merge gate still applies. The factory removes the label when
the head changes or the pull request closes.
"""


def add_parser(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = commands.add_parser(
        "allow-self-review",
        help=HELP.splitlines()[0],
        description=HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("what", help="pr:N")
    parser.add_argument("--yes", "-y", action="store_true", help="do not ask first")
    parser.add_argument("--repo", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    for flag in ("--json", "--verbose"):
        parser.add_argument(
            flag, action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS
        )


def _person(context: Context) -> str:
    """The signed-in GitHub person, or ``""`` for a bot or an Actions token."""
    try:
        user = obj(context.github.api("user"))
    except GitHubError:
        return ""
    login = string(user, "login")
    allowance = runtime.module("factory_self_review_allowance")
    actor = {"actor": {"login": login, "type": string(user, "type")}}
    return str(allowance.human_actor(actor))


def cmd_allow_self_review(context: Context, arguments: argparse.Namespace) -> Outcome:
    console = context.console
    kind, number = _split_target(arguments.what)
    if kind != "pr":
        raise UsageError("allow-self-review takes a pull request: pr:N")
    label = runtime.module("factory_self_review_allowance").LABEL
    person = _person(context)
    data: dict[str, object] = {"pr": number, "label": label, "actor": person}
    if not person:
        console.fail(
            "Only a person may allow self-review. This GitHub login is a bot or an Actions token."
        )
        console.line(f"Add {label} to PR #{number} in GitHub, or run baste with your own login.")
        return Outcome(Result.FAILED, EXIT_CODES[Result.FAILED], data)
    console.line(f"PR #{number}: let the head's author review it, as {person} (this head only).")
    if not arguments.yes:
        if not context.interactive:
            console.line("Nothing was changed. Add --yes to add the label.")
            return Outcome(Result.NO_WORK, EXIT_CODES[Result.NO_WORK], data)
        if not context.prompt.confirm("Allow self-review on this PR?", default=False):
            console.line("Nothing was changed.")
            return Outcome(Result.NO_WORK, EXIT_CODES[Result.NO_WORK], data)
    context.github.repo_api(f"issues/{number}/labels", method="POST", fields={"labels[]": label})
    report = review.inspect(context.github, number)
    data["report"] = report.to_dict()
    if not report.self_review_allowed_by:
        console.warn(f"Label added, but the factory does not accept it yet: {report.reason}")
        return Outcome(Result.FAILED, EXIT_CODES[Result.FAILED], data)
    console.ok(f"PR #{number}: self-review allowed by owner (label by {person})")
    return Outcome(Result.COMPLETED, EXIT_OK, data)
