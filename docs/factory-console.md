# Factory operator console

## Local web control room

The browser interface uses the same `rotisserie factory` commands and your existing `gh`
authentication. It does **not** install or start an additional controller:

```bash
rotisserie web --repo JoshCLWren/comic-pile
```

This opens `http://127.0.0.1:8765/`. Omit `--repo` from a checkout containing
`.rotisserie/config.toml` or a GitHub `origin` remote. Use `--port 0` to
choose an available local port, or `--no-browser` to suppress automatic opening.

The dashboard shows current workers, merges, queue eligibility, health,
provider/model discovery, reliability, outcomes, leases and settings.
The launch form supports next eligible work, a chosen issue or PR, exact model
IDs, normal/forced scheduling, explicit takeover and optional worker slots.
PR actions include independent review, CI repair, review feedback and conflicts.

**Operator actions are real**: Launch, Cancel, Prioritize, Sync, Unstick,
Update Setting, Doctor Fix and Release Expired Leases call the existing
CLI under a bounded allowlist. The browser explicitly asks for confirmation;
the factory remains responsible for lease, authorization and CI guards.
Launch uses `--no-wait`, so *submitted* means GitHub accepted the request,
**not** that the model started; use the linked Actions run or Workers page
to follow it. Sync defaults to leased/queued mode, never `--now`.
Unstick previews from live state, then replans when executed.

The server only binds IPv4 loopback, checks the localhost Host/Origin, requires
a per-process session token for every API request, does not accept shell
commands, and never sends `gh` credentials to the browser. It is a single-user
local operator tool, not a public, remotely hosted multi-user dashboard.
Stop it with Ctrl+C. If GitHub or the installed factory is unavailable,
panels show a failure instead of inventing state.


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
rotisserie factory checks pr:N # why its checks fail, in one line each
rotisserie factory review pr:N # whose approval counts: known self-review vs missing metadata
rotisserie factory groom inventory  # the whole open backlog, with evidence per item
rotisserie factory doctor      # is the factory itself healthy
rotisserie factory disk [--fix --yes]  # what fills the host's disks; reclaim the safe part
rotisserie factory conflicts pr:N  # which of its files main also changed, and where
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
| `--take-over` | if another worker holds the target, stop it first, then start yours; waits for the scheduler's decision (even with `--no-wait`) and takes over once more if a routine pass re-claimed the item in between |
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

`--skip pr:N` keeps an item out of the plan and `--only pr:N` limits the plan to
the named items (both repeatable). Every run plans again from live state, so a
plan seen with `--dry-run` can differ a minute later; to start exactly what
you reviewed, pass those items with `--only`.

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
| `ci-repair` | the PR's checks pass on its current head |
| `review-findings` | the PR left the changes-requested stage |
| `review` | the PR has a review decision (ready or changes requested) |
| implement (issue) | the worker delivered a pull request |

A failed worker never counts as done, even when later work fixed the item.
When a worker's change was rejected by a factory gate (for example the fast
checks), the row says which one; that detail is otherwise only in `/tmp` on
the runner.
Requests the scheduler deferred count as "not started", not as misses. The
"Not done" list shows each item whose latest started request missed its goal.
A PR someone closed counts as "closed" (overtaken), not as a miss.

`outcomes --by-item` shows one line per item: rounds, misses, actions and the
latest result, most-missed first. An item that missed twice or more with
nothing done is marked "going in circles": another round on the same models
is unlikely to help. `--limit N` reads more scheduler passes (default 40,
newest first) for long windows.

## Whose review counts?

The owner's rule is narrow: **a model known to have authored a PR must not
approve it.** One eligible approval is always enough.

- Known authorship comes only from the controller's author records (what the
  runtime actually executed), across every head of the PR, so a sync or merge
  without a new author record never launders an earlier author away.
- The reviewer's model is the controller's record of what the runtime executed
  for that approval, never a worker's historical roster slot. A worker that
  switched models may review its own earlier PR with the new model.
- Models are compared by canonical identity: provider, route and pricing-tier
  spellings of the same weights (`opencode-go/hy4-flash-free`, `zen/hy4-flash`)
  are one model; `glm-4.6` and `glm-4.5` are different models.
- No recorded author (a human, an external tool such as ChatGPT or Claude Code,
  an unattributed head): any model may review. Missing metadata is reported,
  never a blocker.
- A known self-review, or an approval whose model was not recorded while the
  PR has known authors, sends the PR back to `factory:review` for a different
  model. The dispatcher also refuses to start a review on an author model: it
  re-picks across the fleet before the session starts.

`rotisserie factory review pr:N` shows the known author models, whether the
current head has its own author record, the approvals and their recorded
models, and the verdict. `--blocked` checks every open factory PR parked in
`factory:blocked`. `--requeue` (confirm, or `--yes`) moves PRs the retired
review gate parked there back to review, together with their linked issue. It
refuses a PR blocked for any other reason, one a worker holds, or one whose
head moved since it was inspected, and it never writes authorship markers.

## Why are its checks failing?

`rotisserie factory checks pr:N` reads the failing check runs on the PR's
current head and pulls the first real error out of each log: a `cargo fmt`
diff (with the files), failing test names (and a hint when the failure is a
stale checked-in generated contract), or a compile error. A review-feedback
round cannot validate while these are red, so `unstick` plans **ci-repair**
instead of review work for such PRs, and for one a worker already holds it
says to take it over (`run … --action ci-repair --take-over`).
`--since 2h` sets the window; `--all` also follows routine scheduler passes,
one row per worker each pass started.

The factory merges a PR only when every check reported on its exact head is
green, not just the ones branch rules require. Rules often require one
aggregate check while path-filtered workflows (a Rust build that runs only when
Rust files change) report beside it; a red one of those still blocks the merge,
and a pending one waits. A check that is purely advisory can be exempted by
listing its exact name in the `FACTORY_ADVISORY_CHECKS` environment variable
(comma-separated) of the merge workflows. Nothing is exempt by default.

### Failing because of main

A PR is tested as if merged into the default branch, so a breakage that lands
on main turns every open PR that runs the same check red. Repairing it PR by PR
wastes workers: fixing main's files is outside the PR's scope (the factory
rejects it), and leaving them keeps the check red.

`checks`, `unstick` and `doctor` tell the two apart, from GitHub alone and with
no repository-specific names:

- **File evidence**: the failure names files (a formatter diff, a compile
  error's path) that the PR does not change.
- **Main evidence**: the same check's latest run on the default branch, found
  through the failing run's own workflow, and whether it fails the same way. A
  green result older than the change that broke the files is reported as out of
  date (merges made with the workflow token start no push CI).
- **Spread**: failures naming the same files are one incident, even across
  checks (two workflows running the same formatter).

Each failing check is **own**, **inherited** (main still fails), **fixed on
main** (main changed the files, or the check passed on main, after the PR's
run) or **mixed** (fails in the PR's own files and in main's).

- While main still fails, `unstick` sends no worker to PRs whose failures are
  all inherited or mixed, and lists the incident under "Failing because of
  main" with the likely cause (the main commit, and its PR, that last changed
  the files). `doctor` fails the "shared failures" check. Fix main once.
- Once main has moved past a failure, the repair is merging main into the
  branch. `unstick` plans a **sync** for each PR no worker holds (GitHub's
  update-branch, refused if the head moved since the plan or a worker took the
  PR); its own failures get a worker after the checks run again. `doctor --fix`
  makes the same syncs. PRs a worker holds are left alone.
- If a PR's changed files cannot be read, its failures count as its own.

## Syncing branches with main

`rotisserie factory sync pr:N [pr:N ...]` or `rotisserie factory sync --all-stale`
merges the default branch into pull requests. Rotisserie owns this operation: it
replaces the hand-run `sync_watch.py` watcher, which is retired and must not be
restarted. One implementation (`factory_branch_sync.py` in the factory runtime)
serves the scheduler, `sync`, `unstick --yes` and `doctor --fix`.

- **Ownership comes from Neon leases.** A sync first takes a short (5 min)
  operation lease on the PR's Neon target key, which is the key a worker claim
  checks. No worker can be assigned the PR mid-sync, and a PR a worker holds is
  never touched. Owner labels are used only where Neon cannot be reached, and
  then every result says "Neon leases were not consulted".
- **Never overwrites work.** The write is GitHub's update-branch with
  `expected_head_sha`: if a worker pushed since the snapshot, GitHub refuses and
  the result is `head-moved`. It never force-pushes and never pushes to the
  default branch.
- **Conflicts go to merge-conflict work.** A conflicting PR is reported as
  `conflicts` and left for the scheduler's merge-conflict routing. It isn't
  retried.
- **Results** per PR: `synced`, `held-by-worker`, `already-current`,
  `conflicts`, `head-moved`, `closed` or `error`, each with a sentence of text.
  `--json` prints them as data.
- **Where it runs.** On a laptop without Neon, `sync --yes` *queues* each PR
  with the `factory-sync-main` label. The dispatcher's routine pass, "Sync queued
  factory PRs with main under Neon leases", performs queued syncs under a lease
  (at most 5 writes per tick) and removes the label once a PR is synced,
  current, conflicting or closed. `--now` syncs right away on owner labels
  alone. `--contains SHA` treats a PR whose head already has that commit as done.
  `--all-stale` picks the PRs `unstick` would sync: red only because main has
  since moved past the failure, not conflicting.
- **GitHub cost.** A pass reads the open-PR list once and passes it to every
  step. Each candidate then costs one compare, plus one update-branch (and, for
  a queued PR, one label removal) when it's written. The scheduler tick is one
  paginated list call plus about 3 calls per PR it syncs. A tick with nothing
  queued costs one list call.

`rotisserie factory leases [pr:N ...]` shows who holds what: every worker and
target lease row from Neon (active or expired, with its claim and holder) where
Neon is reachable (`FACTORY_STATE_DSN_FILE` set and the ledger importable).
Otherwise it says leases are visible only to the trusted runtime and shows owner
labels instead. `leases --release-expired` deletes lease rows that expired over
an hour ago. Expired rows already block nothing, so this is cleanup only and
can never take an active claim. Stranded *active* claims are released by the
dispatcher's stale-lease reconciler, not by this command.

## What does it conflict with?

`rotisserie factory conflicts pr:N` compares each file the PR changes (files it
added are skipped) at the PR's merge base with the default branch, and names
the merged PR behind each difference. It is exact for any branch age; the
compare API's file list stops at 300 files, and commit `since` filters miss
merged commits that keep older dates.

`unstick` adds this overlap to every merge-conflict task. Once three different
models have failed an item's conflicts in the last 6 hours, it stops planning
workers for it and lists it under **Needs you: models keep failing**: merge it
by hand, or close it if the overlapping merged PR already did the work.

## Grooming the backlog

`rotisserie factory groom` reconciles an adopter's open issues and PRs with
reality in two steps, with a person (or an agent working for one) between them.

```
rotisserie factory groom inventory --out inventory.json   # read everything, attach evidence
rotisserie factory groom apply plan.json                  # check a reviewed plan (dry run)
rotisserie factory groom apply plan.json --yes --log groom-audit.jsonl
```

**inventory** reads every page of open issues and PRs and refuses, rather
than truncates, if any page cannot be read. Each item gets evidence: who holds
it, the PRs that implement an issue and the issues a PR implements, declared
dependencies (`Depends on #N`, `blocked by #N`) that are already closed, merged
PRs that claim to close or be part of the issue (a bare mention does not
count), near-identical titles, and text about the factory runtime (which
belongs upstream). A disposition is proposed only where evidence alone decides
it: a PR whose issues are all closed is `superseded`, a structured
manual-only issue is `manual-product-decision`. A merged PR is evidence to
verify, never proof that an issue's acceptance criteria are met, so such issues
are `unknown` until someone checks.

**apply** runs a JSON plan of reviewed actions:

```json
{"actions": [
  {"id": "unhold-1234", "op": "edit-body", "target": "issue:1234",
   "why": "the hold on #100 was lifted on 2026-01-15", "body": "...new body...",
   "expect": {"body_sha256": "<from the inventory>"}},
  {"op": "labels", "target": "issue:1234", "why": "...", "remove": ["factory:blocked"]},
  {"op": "close-issue", "target": "issue:99", "reason": "completed",
   "why": "PR #120 delivered every acceptance item; verified on production",
   "comment": "..."},
  {"op": "duplicate", "target": "issue:98", "duplicate_of": 97, "why": "...", "comment": "..."},
  {"op": "close-pr", "target": "pr:130", "why": "...", "comment": "...",
   "expect": {"head": "<reviewed head SHA>"}}
]}
```

Operations: `comment`, `edit-body`, `labels`, `close-issue` (`completed` or
`not_planned`), `duplicate` (posts "Duplicate of #N" and closes), `close-pr`,
and `transfer` (moves an issue that belongs to another repository, such as a
factory-runtime ticket filed in the adopter, with its comments; set `to_repo`). Every action needs a `why`; closures need a comment; body edits need
the hash of the body that was reviewed; closing a PR needs its reviewed head.
Before each write the item is read again, and the action is **refused** when a
worker holds it, when its body, labels or head changed since review, or when it
would close an issue that an open PR still implements (decide the PR first).
Closing a PR never closes its issue: that is a separate action. Each applied
action leaves a marker comment, so running the same plan again changes nothing.
Without `--yes` nothing is written; `--log` appends every result to an audit
file.

## Is the factory itself healthy?

`rotisserie factory doctor` runs the checks behind problems that were first
found by hand. It exits 4 when any check fails.

| Check | Fails when |
|---|---|
| reliability snapshot | `factory-stats.json` was never published, or is over an hour old |
| dashboard publisher | the last `factory-status-pages.yml` run failed; names the step and error |
| scheduler | no scheduler run in 20 minutes |
| rotisserie intake | the intake decision cannot be made, so no fresh issue is admitted |
| worker rejections | one factory gate rejected at least half (and 3+) of recent finished workers; that points at the gate or the repository, not the models |
| main verification | the exact default-branch commit failed product CI (fail), or has no product CI run yet (warn: *unverified*, never green) |
| disk space | warns when a volume on this host is above `[disk] warn_percent` (default 80%); `rotisserie factory disk` says what is safe to reclaim |

A check that cannot reach GitHub says so instead of failing.

`doctor --fix` makes the safe repairs it finds: re-running the stats publisher
(`factory-status-pages.yml`) when the snapshot is missing or stale (GitHub's
15-minute schedule can lag by hours), and dispatching product CI on an
unverified main. That re-reads main first, so it never stacks a second run.
Without `--fix` it only says a repair is available.

### Serialized merges

Workers build and review concurrently, but merges are serialized:

1. Only the Factory Ready Merge Drain merges ready PRs, at most one per run.
   The dispatcher triggers it each tick and never merges itself. A worker's
   canonical merge route goes through the same gate.
2. A merge lands only onto a main commit whose product CI passed (`green`).
   `pending` waits. `unverified` (no run) waits too, and gets CI dispatched,
   because factory merges use `GITHUB_TOKEN`, which fires no push event.
3. Before merging, the route takes an exclusive claim on main's exact SHA (an
   annotated tag `factory-merge-claim/<sha>`; GitHub refuses a second create).
   Two actors that both saw the same green main cannot both merge. A claim older
   than 15 minutes belongs to a crashed run and is reclaimed. Claims are
   deleted after the merge.
4. After a merge, the route dispatches product CI on the new main. The next
   merge waits for that verdict.
5. A `red` main admits only the main-repair lane: ready PRs labelled
   `main-breakage`, still under every normal authorization and exact-head
   gate. A broken main can't deadlock merging.

Product CI defaults to `ci.yml`. Set `FACTORY_MAIN_CI_WORKFLOWS` (comma list
of workflow files) to require more. Each must accept `workflow_dispatch`. A
path-filtered workflow still runs when dispatched. Per workflow, the newest
run on the commit is the verdict, so a re-dispatched run supersedes a flaky one.
Cancelled counts as failed.

The service behind this (`rotisserie.factory_console.service.main_health`)
accepts already-fetched data, so one snapshot can serve several checks.

## Is the host out of disk?

`rotisserie factory disk` shows how full each volume is and what the factory
can take back from it, with sizes. It reads only this host, so it works
outside an adopter repository and needs no GitHub login.

```sh
rotisserie factory disk                      # report: volumes, reclaimable by category, kept items
rotisserie factory disk --all                # also list every kept item, not just the largest ten
rotisserie factory disk --fix                # dry run: what --fix --yes would delete
rotisserie factory disk --fix --yes          # delete the safe items
rotisserie factory disk --keep TAG           # treat a worker tag as live (e.g. a subagent not in the ledger)
rotisserie factory disk --config FILE        # read [disk] from FILE instead of the adopter config
```

It looks at four categories, and deletes an item only when it is safe:

| Category | What | Safe to delete when |
|---|---|---|
| worktree | each directory under `worktree_roots` | no uncommitted or untracked change, no merge or rebase in progress, not locked, `HEAD` is on a remote branch (pushed or merged), idle for `idle_hours`, no live worker tag, no running process uses it |
| target | each `CARGO_TARGET_DIR` under `target_roots`, named by worker tag | its tag has no live worker, idle for `idle_hours`, no running process uses it |
| build | each `build_dirs` match, plus git-ignored `build_names` (`node_modules`, `target`) inside a kept worktree that is idle (dirty or unpushed, but nobody is at work in it) | idle for `idle_hours`, no live worker tag in its path, no running process uses it |
| cache | `caches` (default: uv, pip, npm, pnpm, Go build and Cargo registry caches, found the way each tool finds its own) | no running process uses it |

Everything else is kept, with the reason: a dirty worktree, unpushed commits,
a checkout that is not git, a live worker. A worker is live when its tag (the
worktree or target directory name) has a running pid in the local factory
`ledger` (`<tag> pid=<n> ...` lines), or `--keep` names it. "Running process
uses it" means a process's working directory, an open file or its
`CARGO_TARGET_DIR` is inside the item (read from `/proc`).

`--fix --yes` re-checks each item, including which processes use it, just
before deleting it. It refuses any path that is not under a configured root,
that is reached through a symlink below the root's fixed prefix, or that has a
filesystem (bind mounts included, read from `/proc/self/mountinfo`) mounted at
or inside it; it runs `git worktree prune` after removing a worktree. Idle time
counts the newest change anywhere in the tree, and git runs without inherited
`GIT_DIR`/`GIT_WORK_TREE`-style overrides. An item inside another safe item is
listed (and counted) once, with its parent. The suggested fix command repeats
`--config` and every `--keep`. Without `--yes`, `--fix`
is a dry run. Sizes count only blocks the deletion would free: a file also
hard-linked from elsewhere (uv's cache links into virtualenvs) frees nothing.
It exits 4 when a deletion only partly succeeded.

`doctor` warns in its "disk space" check when any volume is above
`warn_percent` and points here.

Configure it in the adopter's `.rotisserie/config.toml` (or any TOML file
passed with `--config`). Every key is optional. Relative paths resolve against
the adopter repository (or the standalone file's directory); `~` and
`$VARIABLE` expand; roots accept globs.

```toml
[disk]
warn_percent = 80                 # doctor warns above this
idle_hours = 2                    # anything changed more recently is kept
volumes = ["/", "/mnt/data"]      # default: "/" plus every root's volume
worktree_roots = [".claude/worktrees", "/mnt/data/worktrees/*"]   # default: .claude/worktrees
target_roots = ["/mnt/data/cargo-target"]                         # default: none
build_dirs = ["/mnt/data/code/*/target"]                          # default: none
build_names = ["node_modules", "target"]
caches = ["$UV_CACHE_DIR", "~/.cache/pnpm"]                       # replaces the defaults
ledger = "/mnt/data/local-factory/ledger.txt"                     # default: none
```

The service (`rotisserie.factory_console.service.disk`) returns a typed
`DiskReport` and `Removed` results; the CLI is one renderer of them.

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
