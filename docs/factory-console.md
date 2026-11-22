# Factory operator console

`rotisserie factory` operates a software factory installed from `factory/`
(for example ComicPile). It replaces adopter-local scripts such as
ComicPile's old `scripts/factory-run`.

```bash
rotisserie factory run opencode-go opencode-go/hy4-flash-free --force
rotisserie factory status      # who is working on what: provider, model, state, how long
rotisserie factory report      # what merged, what failed and why, is it running
rotisserie factory stats       # which providers and models actually deliver
rotisserie factory settings    # limits, worker counts and priority order
rotisserie factory why issue:N # why an issue is or is not being picked up
rotisserie factory unstick     # start the workers that clear the bottleneck
rotisserie factory outcomes    # did the requested work actually happen
rotisserie factory close pr:N --reason TEXT  # close work that should not be done
```

The repository is taken from `--repo owner/name`, `FACTORY_REPO`, the nearest
`.rotisserie/config.toml`, or the `origin` remote, in that order. The console
uses your existing `gh` login (it needs `repo` and `workflow` access).

## Install

Rotisserie is private, so install it from GitHub with your SSH access:

```bash
uv tool install "git+ssh://git@github.com/JoshCLWren/rotisserie.git"
rotisserie factory --help
```

Upgrade with `uv tool upgrade rotisserie`.

## Starting work

`rotisserie factory run` with no arguments asks for the provider, model and
work in menus. In the issue and pull request lists, items someone is already
working on show the provider and how long it has been running. Picking one
offers to **take it over** with your provider and model, or to **stop** that
work.

With both a provider and a model on the command line, it starts at once and
asks nothing.

| Option | Meaning |
| --- | --- |
| `--target issue:N` / `pr:N` | work on this item instead of the next task |
| `--force` (alias `--force-slot`) | override the factory's own limits (below) |
| `--slot N` | use worker number N for the provider (implies `--force`) |
| `--action ACTION` | on a chosen PR: `review`, `review-findings`, `ci-repair`, `merge-conflict` |
| `--take-over` | if another worker holds the target, stop it first, then start yours |
| `--no-wait` | submit and return immediately |
| `--follow` | watch until the worker finishes and show the PR's checks and review |
| `--json` | print one JSON document instead of progress |
| `--verbose` | also print raw diagnostic detail |

### What `--force` does

The person at the console is right. A forced request always becomes work when
any exists. It ignores everything the factory set for itself to pace
automatic scheduling: open-PR and review backlog limits, retry cooldowns,
historical model and worker health, daily dispatch budgets, quota pauses,
OmniRoute caps and the number of workers per provider. When every regular
worker for a provider is busy, it uses an extra worker identity instead of
refusing. For a chosen item it also ignores blocked or dependency labels and
CI still running.

It does not take an item another worker currently holds. That would make two
models write the same change. Use `--take-over`, or `rotisserie factory cancel`.
It also never switches to a different provider or model than the one you
named. If the provider itself refuses (missing credentials, no credit), the
console says so. An item marked manual-only in its description is reported
rather than overridden, because that marker is your own standing instruction.

## Reading progress

By default `run` watches until the model has **actually started working**,
then prints where to follow it. Each line is a real state change read from
GitHub. Nothing is estimated:

```
✓ Request sent  Sat 6:42 PM
• Waiting for earlier factory requests to finish
• Reading open issues and pull requests
• Choosing work and claiming it
✓ Claimed issue #3953  New releases: Fix missing publisher and volume titles
✓ Assigned to OpenCode Go: opencode-go/hy4-flash-free  worker 2
• Worker is double-checking its claim
• Signing in to OpenCode Go
• Checking the model is available
✓ Model started working on #3953  Sat 6:45 PM
```

Being assigned is not the same as working. If the worker stops before the
model starts, the console says "The model never started" and gives the
recorded reason. With `--follow`, a delivered pull request is reported with
its checks, review and merge state, because an open PR is not finished work.

Ctrl+C stops watching only. The request keeps running on GitHub. Resume with
`rotisserie factory watch RUN`, or stop it with `rotisserie factory cancel issue:N`.

## Why isn't it picking that up?

`rotisserie factory why issue:N` answers from the dispatcher's own rules, run
over the same GitHub rows the controller reads:

```
#5612  Closing a dirty Add Series draft does not ask to discard

! Eligible, but held back by the factory's limits
  • 8 pull requests are waiting on review or checks (limit 6,
    FACTORY_REVIEW_BACKLOG_LIMIT). Only main-breakage issues start new work
    until that drains.

Raise the limit:  rotisserie factory settings FACTORY_REVIEW_BACKLOG_LIMIT N
Start it anyway: rotisserie factory run PROVIDER MODEL --target issue:5612 --force
```

It reports blocked, terminal and epic labels, the manual-only marker, open
dependencies (declared in the body and GitHub's dependency links), an existing
pull request, a worker already holding it, the Rotisserie intake decision, and
the open-PR and review-backlog limits. `why` with no argument summarizes every
open issue: how many can start now, and how many are held back for each
reason. `--verbose` adds the dispatcher's reasons verbatim; `--json` prints
them all.

The intake decision runs the installed Rotisserie, which may be newer or older
than the one the factory workflow uses. A specific worker may still skip an
eligible issue it failed on before (strike retry); that is decided at
assignment.

## Clearing the bottleneck

`rotisserie factory unstick` turns `why`'s diagnosis into work. It reads the
same board, then plans:

- **Review backlog at its limit** (`FACTORY_REVIEW_BACKLOG_LIMIT`): new issues
  cannot start, so it targets the backlog PRs no worker holds. Reviews come
  first (an approval moves a PR out of the backlog), then CI repairs, review
  feedback, and merge conflicts. It says how many PRs must leave the backlog
  before new issues can start.
- **Nothing backed up**: the issues that can start now, but only as many as the
  review-backlog and open-PR limits leave room for. Every started issue becomes
  a PR in that backlog, and the scheduler fills free slots on its own.

PRs a worker already holds are listed, never taken. PRs `main` has moved past
(half or more of the files they change no longer exist on the default branch,
for example after a program was retired or code moved to another repository)
are listed under "Needs you: probably obsolete", with the commit that removed
the first file. No worker is sent to them: a merge-conflict worker cannot
resolve changes to deleted files. Close them to free their backlog slots. Each task gets the
best-ranked model that has not missed on that same item in the last 6 hours
(from `outcomes`), so a model that just left a conflict in place is not sent
back to it. Models are ranked by delivered track record over 7 days, from
the published reliability counts: set up in the repository, not paused, at
least 3 runs, at least one PR delivered. Name `PROVIDER MODEL` to choose them
yourself.

```
rotisserie factory unstick --dry-run   # show the plan only
rotisserie factory unstick             # show it, ask, then start
rotisserie factory unstick --max 3 -y  # start at most 3 without asking
```

Starting sends one forced, targeted request per task (the same as
`run PROVIDER MODEL --force --target pr:N --action ACTION`). Outside a terminal
it starts nothing unless you pass `--yes`.

## Closing work that should not be done

`rotisserie factory close pr:N [pr:M …] --reason TEXT` is for work you
cancelled, or that `main` has moved past (see `unstick`'s "probably obsolete"
list). It shows what it will do and asks first (`--yes` skips the question;
outside a terminal it closes nothing without it). For each pull request it:

1. stops the worker holding it, if any, and waits for the claim to be released;
2. closes the PR with your reason as the comment;
3. closes the issue it delivers as **not planned**, with the same reason, so
   intake does not pick it up again (`--keep-issue` leaves the issue open).

## Did it work?

A worker's own verdict is not proof. A merge-conflict worker can finish
"without changes" while the pull request still conflicts.
`rotisserie factory outcomes` follows each targeted request (from `run --target`
or `unstick`) through to its effect: what the scheduler decided, what the
worker reported, and whether the goal holds **now**:

| Action | Goal checked |
|---|---|
| `merge-conflict` | GitHub no longer reports the PR as conflicting |
| `ci-repair` | the PR left the failing-checks stage |
| `review-findings` | the PR left the changes-requested stage |
| `review` | the PR has a review decision (ready or changes requested) |
| implement (issue) | the worker delivered a pull request |

A failed worker never counts as done, even when later work fixed the item.
Requests the scheduler deferred count as "not started", not as misses. The
"Not done" list shows each item whose latest started request missed its goal.
A PR someone closed counts as "closed" (overtaken), not as a miss.
`--since 2h` sets the window; `--all` also follows routine scheduler passes,
one row per worker each pass started.

## Other front ends

The console's logic lives in `rotisserie.factory_console.service`, which never
prints, prompts or parses arguments. It returns typed results with `to_dict()`
and structured reason codes (`Reason.code`, plus the issues, PRs and setting
involved), so a TUI, web page or MCP server can call it directly:

```python
from datetime import UTC, datetime

from rotisserie.factory_console.github import GitHub
from rotisserie.factory_console.service import providers, queue, unstick

github = GitHub("owner/repo")
now = datetime.now(UTC)
summary = queue.summarize_queue(
    github, now=int(now.timestamp()), variables=providers.repository_variables(github)
)
plan = unstick.plan(github, now=now)
print(plan.to_dict())
```

`queue` (why), `unstick` (plan and start), `outcomes` (did it work),
`catalog` (models offered now), `dispatch` (submit a request),
`reliability` (track record) and `providers` (setup) are in the service so far.
Older commands still mix reading, deciding and printing in `commands.py` and
move into the service when they are next changed.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | accepted, started, or finished |
| 2 | used incorrectly (unknown provider, bad model ID, bad target) |
| 3 | no work assigned: nothing waiting, or the item is held by another worker |
| 4 | the scheduler, worker or model failed |
| 5 | GitHub unreachable, not logged in, or rate limited |
| 6 | the outcome could not be confirmed |
| 130 | you stopped watching; the factory keeps working |

## Reliability and the daily report

The factory's dashboard job publishes `factory-stats.json` to the
`factory-status-live` branch every 15 minutes. It holds counts of finished
worker runs per provider and model (delivered, finished, idle, failed, and
failure reasons) from the trusted outcome ledger. `stats` and `models` show
those counts. `report` combines them with what merged, the most common recent
failure reasons, whether the scheduler is still running, and main's checks.

## Settings

Every pacing number lives in one registry,
`factory/.github/scripts/factory_settings.py`. `settings NAME VALUE` validates
the value and stores it as a repository Actions variable. Every factory
workflow receives the variables as `FACTORY_REPO_VARS`, so a change applies to
the next run without a commit. `settings NAME --reset` restores the default.
Priority order (`FACTORY_PRIORITY_ORDER`) decides which kind of work is
picked first. `prioritize issue:N` raises one item within its kind.
