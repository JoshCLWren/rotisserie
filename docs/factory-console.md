# Factory operator console

## Hosted control room (GitHub Pages)

An adopter's hosted dashboard (its GitHub Pages site) is generated from the **same** `src/rotisserie/web_ui.html` and `web_ui.js` as `rotisserie web`.
The public GitHub Pages interface reads sanitized factory snapshots from the
`factory-status-live` branch, and does not use a separate controller or
database. The publishing workflow preserves the previous detailed status HTML
as `legacy-dashboard.html` and publishes `factory-status-snapshot.json`
alongside `factory-stats.json`.

**Snapshots are not live operator evaluations.** Signed out, the page shows
status, merges, health, outcomes and reliability from the published snapshot
with its timestamp. Every other view says "Sign in with GitHub to use this".
The provider and model pickers show published history only, and no
historical model can be chosen.

Signed in, the other views (Work queue and Why?, leases, settings, PR checks,
review and conflicts, item follow, runs and history, incidents, the unstick
preview, and live providers and models with their switches) run the same
`baste ... --json` reads in the adopter's read-only
`rotisserie-operator-read.yml` workflow. The page batches reads that start
together into one run, keeps each result for one minute, and labels it with
its GitHub run and observation time. A Refresh button drops the cached
results. Signing out or an expired session drops them too.

### GitHub administrator access

The hosted dashboard has a **Sign in with GitHub** button. It opens GitHub's
normal browser authorization screen and returns the operator to the same
control room. No personal access token, developer-token creation, or manually
pasted credential is required.

The adopter's sign-in service, named by `[dashboard] auth_url` in
`.rotisserie/config.toml`, supplies only the OAuth callback and a narrowly scoped command-dispatch bridge. The GitHub OAuth
authorization-code flow uses PKCE, a signed, HTTP-only, SameSite=Lax state
cookie, and a server-side code exchange. The GitHub access token and client
secret never enter the public HTML or browser. The bridge verifies GitHub's
`/user` identity and issues a 20-minute signed Rotisserie-only session. The
public page consumes it from a URL fragment, promptly clears that fragment
from browser history, and retains only the short-lived Rotisserie session in
sessionStorage. The session is checked server-side on **every** operator
request and expires automatically.

The existing public snapshots remain completely read-only without login.
Commands from a signed-in administrator go to the sign-in service, which
enforces the GitHub user ID allowlist and the exact Pages origin, then
dispatches the JSON request to `rotisserie-operator.yml` (writes) or
`rotisserie-operator-read.yml` (reads). The workflows enforce the shared
command contract, leases and factory policies. The page waits for the run,
then shows the CLI's own result and the operator run's conclusion: a
rejection such as "Invalid provider/model identifier" appears in the page.
For `run`, the page first checks the model against the live model list and
refuses a model that is not listed, turned off or unavailable.

#### What the sign-in service provides

| Endpoint | Behavior |
| --- | --- |
| `POST <auth_url>/dispatch` | Accept every operation in `WRITE_OPERATIONS` from `control_contract.py`, and `read`. Dispatch `read` to `rotisserie-operator-read.yml` and the others to `rotisserie-operator.yml`, with the request JSON as the `request` input. Return `run_id` and `run_url` from GitHub's dispatch response (API version 2026-03-10 returns `workflow_run_id` and `html_url`). |
| `GET <auth_url>/runs/<run_id>` | Check the session. Read the run and refuse any run that is not one of those two workflows in the adopter repository. Return `run_id`, `run_url`, `status` and `conclusion`. When the run is completed, download the artifact `rotisserie-operator-<run_id>`, unzip its single JSON file (refuse more than 1 MB) and return it as `result`, or `null` when there is none. |

With an older service that returns no `run_id`, a write shows "accepted by
GitHub" only and live reads say the service cannot report results.

The initial administrator is GitHub user ID **4014199** (`JoshCLWren`).
Additional administrators require a trusted configuration update to
`ROTISSERIE_ADMIN_GITHUB_IDS` on the Rust host, not a frontend change.
The GitHub workflow separately restricts the identity of the service account
that dispatches it.

#### Operator workflows and their results

Two installed callers run hosted requests. Both check the dispatching GitHub
user ID against `ROTISSERIE_ADMIN_GITHUB_IDS` before they check out code, and
both validate the request with `src/rotisserie/control_contract.py`, the same
module that `rotisserie web` uses.

| Workflow | Accepts | Token |
| --- | --- | --- |
| `rotisserie-operator.yml` | one write operation (`run`, `claim`, `settings`, ...) | issues, pull requests and actions write |
| `rotisserie-operator-read.yml` | `{"operation": "read", "reads": [...]}`, up to 8 read commands | read-only, plus the factory store (read-only token) |

Each run writes one JSON result and uploads it as the artifact
`rotisserie-operator-<run id>` (kept one day), also when the command is
rejected or fails. A write result holds `request_id`, `operation`, `command`,
`exit_code` and `result` (the CLI's own `--json` output). A read result holds
`request_id`, `exit_code` and `reads`, one entry per read with `name`,
`query`, `command` and `result`. In a public repository any signed-in GitHub
user can download artifacts. The CLI JSON holds no credentials.

#### One-time GitHub OAuth App provisioning

The OAuth client must exist before publishing the hosted sign-in UI.

1. Under GitHub **Settings → Developer settings → OAuth Apps**, register
   `Rotisserie Factory Control Room`.
2. Homepage: the adopter's GitHub Pages URL.
3. Authorization callback:
   `<auth_url>/callback`.
4. Add `ROTISSERIE_GITHUB_OAUTH_CLIENT_ID` and the sensitive
   `ROTISSERIE_GITHUB_OAUTH_CLIENT_SECRET` to the sign-in service's production
   environment, never to GitHub Pages or source control.
5. Confirm the service's `GITHUB_TOKEN` has permission to dispatch
   Actions in the adopter repository, and verify an allowed login, denied
   login, expired session, and authenticated dispatch before cutting over Pages.

The public UI is intentionally deployed **after** the sign-in bridge is
configured and verified. There are no alternate credential prompts or
unrequested fallbacks.

## Local web control room

The browser interface uses the same `baste` commands and your existing `gh`
authentication. It does **not** install or start an additional controller:

```bash
rotisserie web --repo acme/widgets
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

The Work queue page has an item tracker for one issue or PR: Follow (state,
stage, owner, checks, claim labels and the linked issue), Claim runs, History,
Claim and Finish claim. Claim and Finish claim use `--no-wait`: they change the
label and return, and the label claim controller answers on the item. The item
tracker also shows Estimate. The PR workbench reads checks for a PR or for
`main`, and Close PR closes a pull request and the issue it delivers after it
asks for a reason and a confirmation. The Diagnostics page reads the GitHub API
limits and the backoffs, and resets quota backoffs after a confirmation. Both
backoff controls go through the trusted workflows (`--via github`) locally; the
hosted read workflow reads backoffs from the factory store directly. Run history lists the
failed worker runs of the last 10 hours. Test factory store and Back up factory
store start `baste store selftest` and `baste store export` with
`--no-wait`. Migrate store (dry run) starts `baste store migrate --dry-run`,
and Migrate store starts the real copy after a confirmation. Read store run
shows the result of a self-test, backup or migration run by its ID. Store
schema reads `baste schema` (the hosted page runs it in the read workflow), and
Apply store schema runs `baste schema --apply --via github` after a
confirmation. `tests/test_control_parity.py` checks that every `baste`
command has a control in both control rooms, or a reason in
`src/rotisserie/control_parity.py` why it has none. The Diagnostics page starts a
label doctor dry run or repair on GitHub and links the run. The Models page
lists each provider with its switch (on, or off with the reason) and turns a
provider or one of its models off or on. Turning off needs a reason and a
confirmation.

The server only binds IPv4 loopback, checks the localhost Host/Origin, requires
a per-process session token for every API request, does not accept shell
commands, and never sends `gh` credentials to the browser. It is a single-user
local operator tool, not a public, remotely hosted multi-user dashboard.
Stop it with Ctrl+C. If GitHub or the installed factory is unavailable,
panels show a failure instead of inventing state.


`baste` operates a software factory installed from `factory/`
in an adopter repository. It replaces adopter-local operator scripts.

```bash
baste run opencode-go opencode-go/hy4-flash-free --force
baste status      # who is working on what: provider, model, state, how long
baste report      # what merged, what failed and why, is it running
baste stats       # which providers and models actually deliver
baste settings    # limits, worker counts and priority order
baste why issue:N # why an issue is or is not being picked up
baste unstick     # start the workers that clear the bottleneck
baste outcomes    # did the requested work actually happen
baste close pr:N --reason TEXT  # close work that should not be done
baste checks pr:N # why its checks fail, in one line each
baste review pr:N # whose approval counts: known self-review vs missing metadata
baste allow-self-review pr:N  # owner exception: the head author may review this head
baste groom inventory  # the whole open backlog, with evidence per item
baste doctor      # is the factory itself healthy
baste limits      # GitHub API requests remaining and reset times
baste conflicts pr:N  # which of its files main also changed, and where
```

The repository is taken from `--repo owner/name`, `FACTORY_REPO`, the nearest
`.rotisserie/config.toml`, or the `origin` remote, in that order. The console
uses your existing `gh` login (it needs `repo` and `workflow` access).

## Install

Rotisserie is private, so install it from GitHub with your SSH access:

```bash
uv tool install "git+ssh://git@github.com/JoshCLWren/rotisserie.git"
baste --help
```

Upgrade with `uv tool upgrade rotisserie`.

## Starting work

`baste run` with no arguments asks for the provider, model and
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
models write the same change. Use `--take-over`, or `baste cancel`.
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
`baste watch RUN`, or stop it with `baste cancel issue:N`.

## Why isn't it picking that up?

`baste why issue:N` answers from the dispatcher's own rules, run
over the same GitHub rows the controller reads:

```
#5612  Closing a dirty Add Series draft does not ask to discard

! Eligible, but held back by the factory's limits
  • 8 pull requests are waiting on review or checks (limit 6,
    FACTORY_REVIEW_BACKLOG_LIMIT). Only main-breakage issues start new work
    until that drains.

Raise the limit:  baste settings FACTORY_REVIEW_BACKLOG_LIMIT N
Start it anyway: baste run PROVIDER MODEL --target issue:5612 --force
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

### Dependency links

An issue waits on another issue when its body has a `Blocked by #N` or
`Depends on #N` line, or a `Dependencies` heading with a list of `#N` items.
A casual `#N` mention elsewhere in the body does not count. Each dispatcher
pass copies these lines into the store's `factory_dep` table. The factory
also writes its own links there, for example when it splits a large issue
into child issues. A link has one of five kinds:

- `blocks`: the second issue waits until the first one closes.
- `parent-child`: the child waits on every open issue that blocks its
  parent. An open parent does not block its own children.
- `related`, `duplicates` and `supersedes` are notes and never hold work.

`baste why issue:N` lists each link of the issue and where it came from (the
issue body or the factory). When links form a loop, every issue in the loop
waits on itself. The dispatcher does not take any of them, and `why`,
`ready` and the status page show the loop. Remove one link to release it. The
factory refuses to write a link that would close a loop.

## What will it take next?

`baste ready` lists the work the factory can take now, in the order the
dispatcher takes it, with the stage of each item and the next action:

```
Ready work (9)

#  Item     Stage                      Next action      Title
1  pr:5770  factory:review             review           Post-Rust: support story-arc/overall issue-number aliases an
2  pr:5781  factory:review             review           Post-Rust: bulk read/complete actions must be reviewable and
5  pr:5774  factory:changes-requested  review-findings  ComicPile Rust API migration: resource-oriented v2, remove B
```

An item is ready when it is open, has no factory owner and no `agent:NAME`
label, is not parked, cooling or waiting for a split, is admitted by intake,
and no open issue blocks it. The list comes from the same function the
dispatcher walks (`factory_ready.pool`), so `ready`, `next` and the dispatcher
cannot disagree about the order. After the list, a worker still checks its
role, review independence, checks that are still running, GitHub's own
dependency list and the lease.

Cooling, parks and the links the factory wrote live in the store. Without a
store connection, `ready` says so and its answer is not authoritative; use
`baste ready --via github` for the store's view. Each dispatcher pass also
stores its top ten ready items, and the status page publishes them as the
`ready` block of `factory-stats.json`, so an agent with only GitHub access
can read what comes next. The hosted control room shows that list. `--json`
prints one document.

The dependency links and the ready list follow the ideas of
[Beads](https://github.com/gastownhall/beads) (`bd dep`, `bd ready`, MIT
license). Rotisserie reimplements them on its own store and GitHub labels.

## Clearing the bottleneck

`baste unstick` turns `why`'s diagnosis into work. It reads the
same board, then plans:

- **Review backlog at its limit** (`FACTORY_REVIEW_BACKLOG_LIMIT`): new issues
  cannot start, so it targets the backlog PRs no worker holds. Reviews come
  first (an approval moves a PR out of the backlog), then CI repairs, review
  feedback, and merge conflicts. It says how many PRs must leave the backlog
  before new issues can start.
- **Nothing backed up**: the issues that can start now, but only as many as the
  review-backlog and open-PR limits leave room for. Every started issue becomes
  a PR in that backlog, and the scheduler fills free slots on its own.

PRs the loop breaker parked or holds as looping are outside both limits: they
wait on the owner, not on a worker. They count toward a separate cap,
`FACTORY_PARKED_PR_LIMIT` in the job environment (default twice
`FACTORY_REVIEW_BACKLOG_LIMIT`). When that cap is reached, new issues wait, and
`why`, `unstick` and `turning` report "N parked PRs waiting on the owner". The
console finds parked PRs by their park comment, and reads them only when the
review backlog looks full.

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
baste unstick --dry-run   # show the plan only
baste unstick             # show it, ask, then start
baste unstick --max 3 -y  # start at most 3 without asking
```

`--skip pr:N` keeps an item out of the plan and `--only pr:N` limits the plan to
the named items (both repeatable). Every run plans again from live state, so a
plan seen with `--dry-run` can differ a minute later; to start exactly what
you reviewed, pass those items with `--only`.

Starting sends one forced, targeted request per task (the same as
`run PROVIDER MODEL --force --target pr:N --action ACTION`). Outside a terminal
it starts nothing unless you pass `--yes`.

### What the factory clears by itself

The dispatcher runs `factory_self_heal.py sweep` on every pass:

- A PR in `factory:changes-requested` whose newest review verdict names no
  finding goes back to `factory:review`, once per verdict. The next review
  avoids the model that wrote the empty verdict, when a handoff note names it.
- A thread that a bot reviewer started, that the factory answered "**Fixed**
  in `<sha>`", and whose fix commit is in the current head, is resolved. A
  thread a person started or joined, and a rebuttal, stay open.
- A `factory:merge-conflict` PR that GitHub now reports as mergeable gets main
  merged in, at most once per head, never while a worker or person holds it.
  GitHub still reporting a conflict means the change needs code judgment, so
  merge-conflict work takes it.

Each action posts a factory comment on the PR and records an event in the
factory store. `baste heal [--since 24h]` lists them; `baste recap` shows them
under "Fixed by itself".

## Closing work that should not be done

`baste close pr:N [pr:M …] --reason TEXT` is for work you
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
`baste outcomes` follows each targeted request (from `run --target`
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

### What one worker run did

```bash
baste outcome 45635205302          # or the run's URL
baste outcome RUN --follow         # wait for an unfinished run first
```

`outcome` reads every job log of the run on its own, so a long run that
`gh run view --log` truncates still shows everything. It prints the
`[factory-outcome-class]` record (class, source, model, target, kind and
selection), the worker's verdict, each `##[notice]` line, the error
annotation, the job and step that failed, and links to the run and its failed
jobs. `--follow` waits until the run finishes. It retries a dropped or garbled
GitHub answer (such as "unexpected end of JSON input") up to five times in a
row, and it never retries a login, permission, rate limit or not-found error.
The Diagnostics page has Run outcome.

## How long will it take?

`baste estimate issue:N` (or `pr:N`) compares the item with recent
finished factory work and prints three figures. Each figure has a median, the
middle 80% (10th to 90th percentile) and the number of past items behind it:

- attempts to merge: worker leases (controller-claim markers) on the issue and
  its pull requests. The controller writes one lease on both a few seconds
  apart, so one worker's claims less than a minute apart count once.
- time to merge: for `issue:N`, from the first factory claim to the merge; for
  `pr:N`, from the merged pull request's creation to the merge.
- the chance it finishes without a human, with an 80% Wilson interval. A human
  counts when a person merged it, the owner claimed it (`factory:claimed` or
  `factory:local`), it was parked in `factory:blocked`, or the author marker
  says a person wrote the change.

The sample is the `--sample` (default 100, at most 300) most recently updated
closed `factory` pull requests, grouped with their linked issue, kept only
when a factory worker touched them. Similar means the same start (an issue or a
standalone pull request) and the same work category, then the same intake
labels (`bug`, `user-reported`, `code-debt`, `frontend`), then the same issue
size. The command drops size first, then labels, and needs at least six
similar finished items. With fewer it says so and gives no number. Items still
open are not counted, so the chance is optimistic. By default it reads only
GitHub and `factory-stats.json`, never the factory store. The web UI runs it as the
`estimate` read (the Estimate button in the Item tracker).

GitHub shows only the first and last 100 label changes of an item, so the
default can miss an owner claim, and marker comments are only a stand-in for
worker runs. `estimate issue:N --via github` reads exact history instead:

- It starts the adopter's `rotisserie-label-claim.yml` in `estimate-history`
  mode. That job runs one read-only query (a read-only transaction with a
  30-second statement timeout) over `public.factory_state_events`, limited to
  this repository, the last 45 days and 40,000 rows. Model rows that never
  ran (no exit code and no duration) collapse to one counted row per run and
  outcome class before the row cap, so a refusal burst of hundreds of rows
  does not use up the cap. It publishes per-item counts only: no claim UUIDs,
  logins, prompts or failure log tails. This machine never connects to the factory store.
- An attempt is one worker run that reached a model: a model row that ran
  with an attempt class, or a delivered run. A catalog refusal (a model row
  that never ran) or a capacity answer is counted as a refusal and is never
  an attempt. A run with no model row is decided by its terminal class. Runs
  where no model worked are counted separately.
- `src/rotisserie/factory_console/service/estimate_outcomes.py` maps each
  outcome class to attempt, capacity or idle in one table. A class it does
  not know counts as an attempt, and the output names it. An owner claim
  through `factory:claimed` (client `label`) and an operator revocation count
  as a person; an outside agent's claim does not.
- The store replaces only the attempt counts and person signals. Similar items,
  merge facts and `factory:blocked` still come from GitHub. The window covers
  an item when its first factory claim (or its pull request) falls inside it,
  whatever the issue's age. An item with no store run in the window (it
  started earlier, or no run was recorded) keeps its marker count, and the
  output says how many did. A recorded owner claim counts either way.
- If the run cannot start, fails, or returns no usable history (for example,
  an adopter whose installed caller does not list the mode yet), the estimate
  falls back to GitHub and says why. The output always names its source.
- `--no-wait` starts the run and prints its ID; `--run ID` uses that run's
  history later. The web UI's Exact estimate (store) button does both and shows
  the result in the Item tracker. The hosted control room can start the run
  through the operator workflow but cannot show the result until it has a
  signed-in read path.

## Whose review counts?

The owner's rule (2026-11-24) is narrow: **the model that authored the current
head must not approve it.** A model that authored only an earlier head may
review once a later commit by someone else is the head. One eligible approval
is always enough. Long-lived PRs ran out of reviewers under the old rule, which
excluded every model that authored any head.

- The head author comes from the controller's author records for that exact
  head (what the runtime actually executed). Every model those records name is
  excluded, even when they disagree. A head that a factory worker pushed
  without a model record excludes every model recorded on the PR. Without
  either record, the head commit's `Model:` trailer counts.
- A sync (a merge of the default branch with no change of its own, proven from
  GitHub's compare API) carries the author of the commit it merged into, even
  over a `human` record on the merge. A merge that resolved conflicts or
  edited files does not carry an author.
- Fail closed: a merge that cannot be proven either way (an unreadable read, or
  300 or more changed files), more than five syncs in a row, or an unreadable
  head commit excludes every model recorded on the PR.
- `factory_review_exclusion.for_pull` is the one resolver. Reviewer selection,
  the outside-review bridge, the merge gate, `baste review` and `baste unstick`
  use it or the same policy function.
- The reviewer's model is the controller's record of what the runtime executed
  for that approval, never a worker's historical roster slot. A worker that
  switched models may review its own earlier PR with the new model.
- Models are compared by canonical identity: provider, route and pricing-tier
  spellings of the same weights (`opencode-go/hy4-flash-free`, `zen/hy4-flash`)
  are one model; `glm-4.6` and `glm-4.5` are different models.
- No recorded head author (a human, an external tool such as ChatGPT or Claude
  Code, an unattributed head): any model may review. Missing metadata is
  reported, never a blocker.
- A known self-review, or an approval whose model was not recorded while the
  head has an author, sends the PR back to `factory:review` for a different
  model. The dispatcher also refuses to start a review on the head author: it
  re-picks across the fleet before the session starts.

`baste review pr:N` shows the head author (and the sync it was carried
through), the models excluded from reviewing the head, earlier-head authors
that may now review, the approvals and their recorded models, and the
verdict. `--blocked` checks every open factory PR parked in
`factory:blocked`. `--requeue` (confirm, or `--yes`) moves PRs the retired
review gate parked there back to review, together with their linked issue. It
refuses a PR blocked for any other reason, one a worker holds, or one whose
head moved since it was inspected, and it never writes authorship markers.

### The owner's self-review exception

When no other reviewer is available (for example during a production
incident), a person may add `factory:allow-self-review` to one pull request.
While the label stays on, the model that authored the current head may review
and approve it. Every other gate still applies: the exact head, green CI,
acceptance and main health.

- Only a person can grant it. The most recent `labeled` event for the label
  must come from a human GitHub account: not a bot, not a GitHub App and not
  GitHub Actions. If the events cannot be read, the exception does not apply.
- It covers one head. A head committed after the label was added, a
  force-push after it, or a head other than the one the factory announced the
  grant on is not covered.
- The ready-merge drain announces a new grant once on the PR ("Owner allowed
  self-review on this PR (label added by LOGIN)") with a marker that binds the
  head, and records a store event. The PR reconciler and the drain remove the
  label when the head changes or the PR closes.
- Reviewer selection, the outside-review bridge and the merge gate all read it
  through `factory_review_exclusion.resolve`. An approval it admits carries
  "Self-review allowed by owner (label by LOGIN)" in its reason.

`baste allow-self-review pr:N` (confirm, or `--yes`) adds the label as the
signed-in person and checks that the factory accepts it. `baste review pr:N`
shows "self-review allowed by owner (label by LOGIN)". The control rooms run
the same command. The hosted control room runs it with the workflow's token,
which cannot grant the exception, so add the label in GitHub or use the local
console.

## Why are its checks failing?

`baste checks pr:N` reads the failing check runs on the PR's
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

`baste sync pr:N [pr:N ...]` or `baste sync --all-stale`
merges the default branch into pull requests. Rotisserie owns this operation: it
replaces the hand-run `sync_watch.py` watcher, which is retired and must not be
restarted. One implementation (`factory_branch_sync.py` in the factory runtime)
serves the scheduler, `sync`, `unstick --yes` and `doctor --fix`.

- **Ownership comes from store leases.** A sync first takes a short (5 min)
  operation lease on the PR's store target key, which is the key a worker claim
  checks. No worker can be assigned the PR mid-sync, and a PR a worker holds is
  never touched. Owner labels are used only where the factory store cannot be reached, and
  then every result says "store leases were not consulted".
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
- **Where it runs.** On a laptop without the factory store, `sync --yes` *queues* each PR
  with the `factory-sync-main` label. The dispatcher's routine pass, "Sync queued
  factory PRs with main under store leases", performs queued syncs under a lease
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

`baste leases [pr:N ...]` shows who holds what: every worker and
target lease row from the factory store (active or expired, with its claim and holder) where
the store is reachable (`FACTORY_TURSO_URL` set and the ledger importable).
Otherwise it says leases are visible only to the trusted runtime and shows owner
labels instead. `leases --release-expired` deletes lease rows that expired over
an hour ago. Expired rows already block nothing, so this is cleanup only and
can never take an active claim. Stranded *active* claims are released by the
dispatcher's stale-lease reconciler, not by this command.

## What does it conflict with?

`baste conflicts pr:N` compares each file the PR changes (files it
added are skipped) at the PR's merge base with the default branch, and names
the merged PR behind each difference. It is exact for any branch age; the
compare API's file list stops at 300 files, and commit `since` filters miss
merged commits that keep older dates.

`unstick` adds this overlap to every merge-conflict task. Once three different
models have failed an item's conflicts in the last 6 hours, it stops planning
workers for it and lists it under **Needs you: models keep failing**: merge it
by hand, or close it if the overlapping merged PR already did the work.

## Grooming the backlog

`baste groom` reconciles an adopter's open issues and PRs with
reality in two steps, with a person (or an agent working for one) between them.

```
baste groom inventory --out inventory.json   # read everything, attach evidence
baste groom apply plan.json                  # check a reviewed plan (dry run)
baste groom apply plan.json --yes --log groom-audit.jsonl
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

## Is the factory turning?

`baste turning` answers one question: is the factory moving work right now? The
verdict comes first, then the evidence.

- 🍗🔄 **Turning**: a worker started within `--idle` (45 minutes), or there is
  no work and no worker to start.
- 🐢 **Slowing**: `--failures` (3) or more control workflow runs failed in
  `--window` (1 hour). An Entry run counts only when its persist job failed
  before it recorded an outcome; a run that is red for its recorded outcome
  (for example `work_failure`) is a worker result, not a control failure.
  Slowing also names a looping item (reason `item-loops`, next `baste recap`),
  when no control failure comes first.
- 🔥🔥 **Burning**: no worker started for longer than `--idle` while eligible
  work or eligible workers exist. A stopped rotisserie burns the food.
- ❔ **Unknown**: the worker runs could not be read. `--follow` does not alert
  on it and checks again on the next interval.

Each verdict names the most likely reason and the command that digs further
(👉). Burning reasons, in the order they are checked:

| Reason | Means | Next |
|---|---|---|
| store-unreachable | the store step failed on the last dispatcher pass, or Turso reports BLOCKED | `baste store selftest` |
| dispatcher-none | every recent pass had eligible workers and the controller answered "none" for all of them | `baste unstick --dry-run` |
| dispatch-broken | the dispatcher failed to assign or start a worker | `baste doctor` |
| review-limit, wip-limit | the review backlog or open-PR limit holds new work | `baste unstick --dry-run` |
| no-capacity | no worker was eligible: every provider capped, cooling, out of budget or off | `baste backoffs --via github` |
| scheduler-stopped | the dispatcher has not run in 20 minutes | `baste doctor` |
| unknown | none of the above | `baste why` |

The evidence:

- 🧑‍🍳 when the last worker run started and 🍽️ when the last one finished.
  These come from GitHub runs of `free-model-factory-entry.yml`; the store's
  lifecycle events are readable only inside trusted jobs.
- 🎛️ the last `--passes` (3) dispatcher passes that ran the assign step: eligible
  workers, assigned, "none", and the controller's reason when the dispatcher
  logs one ("reason not logged" otherwise), plus the sources the filters
  skipped (daily budget reached, turned off, missing credentials).
- 💥 failed runs of the dispatcher, status pages, label claim, ready-merge drain
  and completion drain workflows in the window, and Entry runs whose persist job
  failed before recording an outcome, with links. Workflows are matched by file path, not run name.
- 🔁 looping items: each open PR that had 4 or more finished worker runs in the
  last 6 hours, at least half of them wasted (a failure, or a run that ended
  with no work), while it stayed short of `factory:ready`. A run that never
  reached a model, or failed for a factory or provider reason (control plane,
  runner environment, quota, throttle), does not count. Each row names the item, its
  runs (time, action, model, outcome class), the most common wasted outcome and
  the next step.
- 🧊 providers whose models are all cooling, 📦 eligible issues, open factory
  PRs and any backlog limit, ⏰ expired holds (store leases; readable only where
  the store is configured), 🗄️ the store step of the last pass and the age of
  the published `db_usage` snapshot.

It reads a bounded set: one page of worker runs, eight dispatcher runs (jobs
and logs for at most `--passes` of them), one page of failed runs, the jobs of
the five newest failed Entry runs, the reliability snapshot and the open
issue and PR lists. Looping items add the worker runs of the last 6 hours (at
most three pages) and the persist-job log of at most 60 of them; a finished
job's log is cached for good once read.

`baste turning --follow [--every 5m]` checks again on an interval and prints
only changes. On Burning it prints a 🚨🔥 alert line and exits 10; on Slowing it
exits 11. The first reading counts, so starting on a burning factory exits at
once. `--notify` also shows a macOS notification (osascript) and does nothing
elsewhere. `--json` keeps plain values (`"verdict": "burning"`), no emoji.

`baste doctor` runs the same logic as its `work turning` check, and
`baste recap` names the longest stretch over 45 minutes without a worker start
inside the recap window. It says 🔥🔥 Burning only when that stretch is still
going and the doctor's `work turning` check fails; otherwise it states the gap. Both control rooms show the verdict at the top
of the overview, above the morning recap.

### Factory state labels

These labels mirror facts the factory already holds. They never start or stop
work: the store and the controllers decide, and the label-triggered workflows
ignore their events. The code that changes the state writes the label with the
workflow token. The label doctor (`baste labels`) fixes the ones it can read.

| Label | On when | Off when | Written by |
| --- | --- | --- | --- |
| `factory:parked` | The loop breaker holds the PR at its head | New head, `factory:override`, a claim, incident work | Self-heal sweep, doctor |
| `factory:needs-split` | The issue is over the size limit or labeled epic, umbrella or tracking | The issue fits, or gets `factory:override` | Dispatcher, doctor |
| `factory:gate-rejected` | The acceptance gate rejected an approval at the PR's head | New head, or the gate passes at the head | Review controller, self-heal sweep, doctor |
| `factory:duplicate` | An older open PR delivers the same issue (put on the newer PRs) | One PR is left | Self-heal sweep, doctor |
| `factory:cooling` | The store holds the PR with retry cooling or review churn (not a park) | The hold expires | Self-heal sweep only |
| `factory:incident-work` | The item is an active incident's issue or one of its open PRs | The incident ends | Self-heal sweep, doctor |

A closed or merged item loses every state label: the sweep finds them with one
search per pass. `factory:incident` is not in this table. It is the owner's
input that declares an incident, so its events still wake work.

### Issue intake holds

The dispatcher's pool pass sends no implement run to an issue in two cases
(`factory/.github/scripts/factory_issue_intake_guard.py`):

- An open PR delivers part of it: a `Part of #N.` line or a
  `factory/work-checkpoints/issue-N/` branch. A closing PR (`Closes #N`) already
  holds its issue. Merged slices do not hold it; the implement run's prompt
  names them and asks for only the remaining work.
- It is too large for one task: its body is over 16,000 characters
  (`FACTORY_ISSUE_BODY_LIMIT`), or it carries an `epic`, `umbrella` or
  `tracking` label. The factory comments once to ask for a split proposal and
  adds `factory:needs-split`. The owner approves each split and creates the
  child issues; the factory never does.

A targeted run, `factory:override` and `factory:claimed` are never held.

### Looping items and the breaker

One rule, `factory/.github/scripts/factory_item_loop.py`, decides whether an
item loops; `baste turning`, `baste doctor`, `baste recap`, both control rooms
and the dispatcher all call it. The defaults are 4 runs and 6 hours
(`FACTORY_LOOP_RUNS`, `FACTORY_LOOP_WINDOW_SECONDS` in the job environment).

The factory acts on it in two places:

- The dispatcher's pool pass holds a looping PR or issue, so no new worker of
  any kind (review, review findings, merge conflict, CI repair, implement) goes
  to it. Its `none` reason names `held: looping`.
- The dispatcher's self-heal sweep parks a looping PR once per head: one
  trusted comment that says why and what to do next, and a `loop-parked` store
  event. The park holds while the PR's head stays the same, in any stage, and
  the `none` reason names `held: loop-parked`. A stage change does not release
  it, because a failed worker's own release flips the stage. A new head
  releases it, and only runs after the park count again.

The park adds the `factory:parked` label. The same sweep removes it when the
hold ends: a new head, an owner `factory:override` label, a `factory:claimed`
claim or active incident work. The label doctor (`baste labels`) fixes it to match
the park at the PR's current head. The label never holds or releases anything;
the store does. A targeted or forced run (`baste run --target pr:N`), an owner
`factory:override` label and a `factory:claimed` claim are never held. A
targeted or forced run on a parked PR ends the park, even if it pushes nothing:
the dispatcher records a `loop-released` store event and removes the label.
Only runs after the release count toward a new park.

A park the owner leaves alone resolves itself. After 12 hours at a parked head,
the sweep sends the PR one forced run on the model with the best track record
for its stage (the highest success floor among proven models in the published
stats; without one, normal selection), and records a `park-retry` store event
keyed on the park time, so the same park never gets a second retry. Later
sweeps judge that run from the worker outcomes in the store:

- A push ends the park by itself (new head). A delivered or successful run
  ends it through the same `loop-released` event as an owner's run.
- A counted run that changed nothing closes the PR. The closing comment gives
  the park's findings and the last review. The branch stays. The linked issue
  gets a comment with a link to the closed PR and goes back to the queue (an
  oversized or umbrella issue then waits for a split, see "Issue intake
  holds").
- A run that never reached an outcome in 6 hours (it did not start, or hit
  only quota or provider limits) gets one more dispatch, then the PR closes.

An owner override, a claim, an outside agent, incident work or a live lease
exempts the PR, as for the park itself. Reopening the closed PR undoes the
closure; the factory does not close the same park twice.

A run that delivered work (pushed repairs or opened its PR) counts as progress
even when its agent then hit the deadline. Runs that failed for a provider or
model-catalog reason, or with no class at all (`provider_failure`,
`unknown_failure`, `model_unavailable`, `model_retired_410`), do not count. A second park at the same head is never posted. Issues are not
parked: the live rule holds a looping issue until its runs age out of the
window. `baste
recap` finds parked PRs by their park comment, however old, and lists each
looping or parked PR under "Needs you" with
the reason and the next step: review it yourself, merge it, or force one more
try with `baste run --target pr:N`.

`baste recap` also lists, under "Needs you", each factory PR merged in the
window whose issue is still open. Merge closure keeps an issue open when the PR
says it is only part of the issue (`Part of #N.` or "does not close #N"), or
names the issue only in its title. That issue stays claimable, so a worker can
take it again. The row says which case applies and how to close the issue when
the merge finished it. Both control rooms show the same rows.

## Is the factory itself healthy?

`baste doctor` runs the checks behind problems that were first
found by hand. It exits 4 when any check fails.

| Check | Fails when |
|---|---|
| reliability snapshot | `factory-stats.json` was never published, or is over an hour old |
| database usage | the published Turso usage is critical (fail) or warn, or over an hour old (warn); it reads `db_usage` from the same `factory-stats.json`, never the Turso API |
| dashboard publisher | the last `factory-status-pages.yml` run failed; names the step and error |
| scheduler | no scheduler run in 20 minutes |
| work turning | `baste turning` says Burning (fail) or Slowing (warn) |
| rotisserie intake | the intake decision cannot be made, so no fresh issue is admitted |
| worker rejections | one factory gate rejected at least half (and 3+) of recent finished workers; that points at the gate or the repository, not the models |
| main verification | the exact default-branch commit failed product CI (fail), or has no product CI run yet (warn: *unverified*, never green) |

A check that cannot reach GitHub says so instead of failing.

`doctor --fix` makes the safe repairs it finds: re-running the stats publisher
(`factory-status-pages.yml`) when the snapshot is missing or stale (GitHub's
15-minute schedule can lag by hours), and dispatching product CI on an
unverified main. That re-reads main first, so it never stacks a second run.
Without `--fix` it only says a repair is available.

### Turso usage and the free plan

The factory moves its state to Turso's free plan (#253). The free plan allows
5 GB storage, 500 M rows read and 10 M rows written per month, and 3 GB of
sync. Row reads count scanned rows. When any quota is exceeded, every database
in the organization answers `BLOCKED` until the month resets. Turso counts
usage per calendar month. The exact reset hour is unverified, so Rotisserie
shows the start of the next UTC month.

```bash
baste db-usage                # last value the status page published
baste db-usage --via github   # fresh read in the label claim workflow
baste db-usage --live --json  # Turso API; trusted jobs with the token only
```

The status page job runs `.github/scripts/factory_turso_usage.py` with the
`FACTORY_TURSO_API_TOKEN` (Platform API token) and `FACTORY_TURSO_URL` secrets.
It adds the result to `factory-stats.json` as `db_usage`. The organization
and database names come from the URL (`{database}-{organization}.<region>.turso.io`,
split at the last hyphen). Set the `FACTORY_TURSO_ORG` or
`FACTORY_TURSO_DATABASE` repository variable when a name contains a hyphen at
the split. The published value holds counts, status and dates only.

| Status | When |
|---|---|
| `ok` | nothing below |
| `warn` | any allowance projected over 70% at month end, the factory database projected over the owner targets (50 M reads, 1 M writes, 250 MB storage), or overages enabled |
| `critical` | any allowance over 90% used now, or Turso reports blocked reads or writes |
| `not_configured` | the token or URL secret is missing |
| `error` | the Turso API could not be read |

Projections are a straight line from the month so far to month end, from at
least one elapsed day. Storage is compared as it stands. Allowances come from
the organization's plan quotas (`/plans`), or the documented free plan when
those are missing. A workflow without the store URL labels the usage "not in
use yet".

`pause_new_claims(report, error)` in `factory_turso_usage.py` returns a pause
when the status is critical or a database error says `BLOCKED`. The dispatcher
saves the published report to `FACTORY_DB_USAGE_FILE`, and `acquire_lease` and
`acquire_slot` raise `ClaimsPaused` at critical usage, so the factory starts no
new work. An operator's explicit target, renewals, releases and outside-agent
claims are not paused by usage; a `BLOCKED` store refuses them on its own.
Unknown, stale or unconfigured usage never pauses.

Both control rooms show it on the Diagnostics page (Database usage). The
hosted page serves it from the published snapshot without sign-in.

### Store schema

```bash
baste schema --via github           # status, read with the read-only store token
baste schema --apply --via github   # run every schema statement now
baste schema --json                 # direct, inside a trusted job with the store
```

The status lists each table and index that `factory-state.sqlite.sql` creates
and whether the store holds it, the stored `_schema_version` against the
code's `SCHEMA_VERSION`, and the last upgrade attempt from the
`_schema_upgrade` table: its result, the objects it created, the workflow run
and the error. `--via github` runs the read in the Rotisserie Operator Read
workflow. `--apply` runs the `schema-apply` mode of the label claim workflow,
which holds the write token. Each schema statement is `IF NOT EXISTS` or
`INSERT OR IGNORE`, so apply only creates what is missing. It also restores an
object that is missing at the current version, which the version-gated upgrade
skips.

| Status | When |
|---|---|
| `ok` | every object exists and the stored version matches the code |
| `missing` | one or more expected tables or indexes do not exist |
| `behind` | the stored version is lower than the code's version |
| `failed` | the last upgrade attempt failed |
| `ahead` | the stored version is higher than the code's version |

The status page job publishes the status as `schema` in `factory-stats.json`.
`baste doctor` reads that value and warns when the status is not `ok` or is
over an hour old. An adopter must re-run the installer to get the
`schema-apply` option in its label claim caller.

### Moving the factory state from Neon to Turso

`baste store migrate` copies the factory's Neon state into the libSQL store
once, before the cutover (#257). It starts `factory-store-migrate.yml` in the
repository's Actions, which reads Neon with the `NEON_API_KEY` secret and the
`NEON_PROJECT_ID` variable and writes with `FACTORY_TURSO_URL` and
`FACTORY_TURSO_TOKEN`. This machine never connects to either database.

```bash
baste store migrate --dry-run      # read and count only; writes nothing
baste store migrate                # copy, then count every copied row back
baste store migrate --run ID       # read an earlier run's summary
```

Run the dry run first. It reports what would be copied and what is skipped,
with a reason for each skipped row. It also reports the number of
`factory-projection-v1` keys, every provider budget row that holds a limit or
a cooldown (the source and the column names only), and the row count of every
Neon factory table. A table that does not exist is reported as absent.

The tool (`.github/scripts/factory_turso_migrate.py`) reads Neon inside one
read-only snapshot. It maps lease, backoff and health reset keys to
`factory_lease`, `factory_backoff` and `factory_repo_state`, and copies events
and the typed tables. Projection, GitHub read and open-list keys stay in Neon.
Expired reservations are not copied. Expired external claims are copied,
because they still hold their targets. An event without an `event_id` gets
`neon:<id>`, so a re-run finds it again.

Each batch is one transaction of inserts that skip a row whose key is already
stored, so a stored row is never overwritten. After each batch the tool
counts the copied keys in the store. The run reports `mismatch` and exits
non-zero when a count does not agree. An interrupted run is started again from
the beginning and copies only what is missing. A completed run writes a
marker event, and a later run reports `already_migrated` and copies nothing:
copying again after the cutover would bring back claims and backoffs that the
factory has released since. Copy before the factory writes to the store, so
the copied events keep their order.

The summary artifact `factory-store-migrate-<run>` holds counts, table names,
row category names and provider source names only.

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

## Outside agents (ChatGPT, Codebuff, Claude Code, a phone)

Any agent can ask for the next meaningful work and reserve it through the same
leases the factory's own workers use:

```bash
baste next --json                       # ranked offers, read-only
baste reserve-next --client chatgpt --json
baste renew UUID --client chatgpt
baste release UUID --client chatgpt
gh workflow run rotisserie-external-claim.yml --ref main \
  -f operation=handoff -f client=chatgpt -f claim_id=UUID -f evidence=PR_URL
baste revoke UUID --reason TEXT         # operator: remove a claim
```

Handoff always runs in the bridge workflow, which records the head's author
model so it can never approve its own work. `run --take-over` also removes an
outside agent's claim on the target. Agents
with only GitHub access use the installed **Rotisserie External Claim
Controller** workflow. Full rules: [external-agent-claims.md](external-agent-claims.md).

### Label claims from the console

These commands drive the label claim controller (#191) and need no store connection
connection on your machine. Each one waits for the controller's own answer:

```bash
baste claim pr:N                # add factory:claimed, show accepted/rejected
baste finish pr:N               # remove it, show the hand-off or release
baste wait-for-work --claim --via github   # wait, then claim the first item
baste runs pr:N                 # the controller's recent runs and results
baste follow pr:N               # print each stage change until done or held
baste follow pr:N --once        # where it stands now
baste labels --via github [--repair]       # label doctor report
baste leases pr:N --via github  # who holds it in the factory store
baste why issue:N --via github  # eligibility plus the store holder
```

`wait-for-work` never offers a review of a PR you authored, because GitHub
refuses that review. `claim`, `finish` and `labels --via github` take
`--no-wait` to start and return at once (the web UI uses that form). Any
GitHub failure now names the request that failed (`failed_request` in
`--json`), without field values.

### Handing an item to an outside agent

```bash
baste assign pr:N --agent claude                 # claim, label agent:claude, post the task
baste assign pr:N --agent chatgpt --task-file task.md
baste assign issue:N --agent freebuff --kind implement
baste assigned                                   # who holds what, and for how long
baste finish pr:N                                # the agent ends it; removes agent:NAME too
```

`assign` reuses your label claim, so factory workers stay off the item. Then
it adds the `agent:NAME` label (it creates the label when it is missing),
removes any other `agent:*` label, and posts one task comment. Without
`--task` the task follows the item's stage: review, merge-conflict, ci-repair,
review-findings or implement. It adds the files main also changed (as
`baste conflicts` shows them), the failing checks (as `baste checks` shows
them) and the linked issue's acceptance criteria. Each task tells the agent
to put a `Model:` line on its commits or review, and to end with
`baste finish`. A PR at another stage (building, ready or blocked) has no
default task, so `assign` refuses it unless you pass `--kind` or `--task`.
When the controller refuses the claim, nothing is labelled.
A repeated `assign` with the same task does not post it again. The web UI's
Item tracker has Assign to agent and Agent holds.

### Resetting one model's health

```bash
baste health reset nvidia/moonshotai/kimi-k3.1-pro --via github
baste health reset MODEL --source nvidia --via github
```

`health reset` deletes one model's health bench rows and model backoff rows,
for every source or only `--source`, through the trusted model health reset
workflow. MODEL is the ID that `baste backoffs` lists, or the `source/model`
form that run titles print. It lists each deleted row. Worker, provider and
pool cooldowns stay, retired (HTTP 410) records stay, and so does every other
model's state. The repository's reset
marker stays too, so a new failure of this model can still count its recent
failures. `backoffs reset --quota-only` is the repository-wide reset. An
adopter whose reset caller predates the `model` input gets "re-run the
Rotisserie installer". The Diagnostics page has Reset one model.

### Pausing the factory for maintenance

```bash
baste pause --reason "store migration"   # turn off dispatch, claims and state writers
baste resume                             # turn back on what pause turned off
```

`pause` turns off the dispatcher, dispatch recovery, the compatibility
dispatcher, the roster successor, the label and external claim controllers,
the runtime observer, check projection and the status pages. It records the
reason, the time and the list in the `FACTORY_PAUSED` repository variable.
`status`, `turning` and `recap` then start with "paused since TIME: reason".
Running work is not cancelled. `resume` turns on only the workflows in that
list, so a workflow that was already off stays off, and it lists each one.
Both need your own `gh` login with the workflow scope
(`gh auth refresh -s workflow`), so they are CLI only: neither control room
has a Pause or Resume button, because the operator workflow's Actions token
cannot turn workflows off or write the variable. While paused, `assign`
refuses, because no claim controller answers.

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
`reliability` (track record), `providers` (setup), `labels` (label doctor) and
`claimops` (label claims, controller runs, following an item) are in the service so far.
Older commands still mix reading, deciding and printing in `commands.py` and
move into the service when they are next changed.

## GitHub API limits and retry times

`baste limits` shows REST core, GraphQL, and search quotas for the
**authenticated local GitHub CLI credential**, including used/remaining requests,
local reset times and countdowns. `baste limits --json` exposes the
same resource counters and UTC reset timestamps as structured data. Factory
worker GitHub App installation tokens may have **different limits**, so
`limits` also shows the factory App installation's REST budget. It reads the
"Factory installation REST budget" line that the newest successful dispatcher
run's quota gate printed, with its age. It warns near the dispatcher's
1,200-request reserve, below which the dispatcher skips dispatch, and it says
when that reading is stale because its reset time has passed. It costs this
credential about three REST calls, so it is skipped when this credential's own
core quota is spent.

If GitHub rate-limits an operation, Rotisserie consults the quota endpoint and
prints the earliest **known** retry time. A primary REST quota reset is shown
only if the REST core bucket is confirmed exhausted. For secondary throttling,
the server's Retry-After is used when available; otherwise the retry time is
**unknown**. A quota reset is not a promise that GitHub will lift a separate
secondary limit. If even the quota endpoint fails, the CLI reports that instead
of guessing an ETA. `doctor` stops querying GitHub after a throttle and marks
later health checks as **not checked**, not as unhealthy. `doctor --fix` does
not attempt repairs until it can read GitHub.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | accepted, started, or finished |
| 2 | used incorrectly (unknown provider, bad model ID, bad target) |
| 3 | no work assigned: nothing waiting, or the item is held by another worker |
| 4 | the scheduler, worker or model failed |
| 5 | GitHub unreachable, not logged in, or rate limited |
| 6 | the outcome could not be confirmed |
| 10 | `turning --follow`: the factory is Burning |
| 11 | `turning --follow`: the factory is Slowing |
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

## Turning providers and models off

```sh
baste providers disable local-ollama --reason "fans too loud"
baste providers enable local-ollama
baste models disable opencode-go opencode-go/hy4-flash-free --reason "loops"
baste models enable opencode-go opencode-go/hy4-flash-free
baste providers          # the Switch column shows what is off and why
```

The switch writes two repository variables. Every factory workflow receives
them in `FACTORY_REPO_VARS`, so the next dispatcher tick obeys a change without
a deploy.

- `FACTORY_DISABLED_PROVIDERS`: a comma list of provider names, for example
  `local-ollama,kilo-auto`. `opencode-go` is stored as `opencode-free`.
- `FACTORY_DISABLED_MODELS`: a comma list of `PROVIDER=MODEL` pairs. MODEL is
  the ID that `models PROVIDER` shows.
- `FACTORY_DISABLED_REASONS`: JSON with each reason and time. Only the console
  reads it. Workflows never receive it.

Names and model IDs are checked on write. A double quote in a value would
break the `FACTORY_REPO_VARS` JSON, and then every setting and switch falls
back to its default. Edit these variables with the console, not by hand.

Every place that picks a provider or a model obeys the switch:

- roster offers (`factory_dispatch_credential_filter.py`);
- worker assignment, including forced runs and automatic targets
  (`worker_skip_reason` in `factory-work-controller.py`);
- automatic model choice and same-run failover, through the trusted model
  exclusion snapshot (`export-model-exclusions`);
- cross-provider failover, through `export-fleet-failover-sources`;
- `unstick`, which never plans a turned-off provider or model;
- the local Ollama review offer in the dispatcher, and the Local Ollama Review
  workflow itself on every trigger (`local-ollama`);
- PR-Agent advisory review (`routeway`, `requesty`, `orcarouter`).

A run that names the provider (`run PROVIDER ...`) or the exact model may still
use it. A disable never cancels work that is already running. To stop it, run
`baste cancel issue:N` (or `pr:N`).

Dispatch by worker ID (`fixed-model-factory-dispatch` with `worker=`) obeys
the switch: a worker ID does not name the provider. OmniRoute chooses its
upstream providers itself, so only `omniroute-free` as a whole can be turned
off. The provider health probes and the DeepSeek auth comparison check
providers but never pick one for work, so they ignore the switch.

The local overnight scripts (`factory/scripts/opencode-factory.sh` and
`opencode-model-scout.sh`) run on your own machine with their own
`FACTORY_ALLOWED_PROVIDERS` list. They do not read repository variables.

### Adding a provider

A new provider is a Rotisserie code change, not a switch. The former
`factory/defaults/providers.toml` overlay was removed in #83.

1. Add a `ProviderSpec` to `PROVIDERS` in
   `factory/.github/scripts/factory_provider_registry.py` (source key, OpenCode
   provider ID, secret name, OpenAI-compatible endpoint, display name).
2. Add `FACTORY_HAS_<SECRET>: ${{ secrets.<SECRET> != '' }}` to both dispatch
   steps in `.github/workflows/fixed-model-factory-dispatch.yml`, and declare
   the secret in its `workflow_call` secrets.
3. Declare and pass the secret in `free-model-factory-entry.yml` and
   `free-model-factory-run.yml`, and in the callers
   `factory/install/callers/fixed-model-factory-dispatch.yml` and
   `free-model-factory-entry.yml`.
4. Set the secret in the adopter repository.

Worker identities derive from the registry, so the new provider gets worker
slots on the next tick after the adopter runs the new Rotisserie ref.

## Incidents: send free workers to one issue

```sh
baste incident start issue:239 --reason "checkout is down"
baste incident                      # list incidents and their state
baste incident stop issue:239
```

An incident is an issue that must be fixed before anything else. The console
writes two repository variables and labels the issue `factory:incident`:

- `FACTORY_INCIDENTS`: a comma list of `ISSUE:ATTEMPTS` entries, for example
  `239:3`. Every factory workflow receives it in `FACTORY_REPO_VARS`, so the
  next dispatcher tick obeys a change without a deploy. A malformed entry is
  ignored, so a bad value cannot pause the factory.
- `FACTORY_INCIDENT_NOTES`: JSON with each reason and start time. Only the
  console reads it.

An incident is active while its issue is open and still carries the
`factory:incident` label. Removing the label ends it, so a reopened issue does
not restart an ended incident. While it is active:

- the issue and every PR linked to it rank ahead of all other work, owner picks
  (`factory:override`) and main-breakage included, for every worker;
- the issue skips the WIP, review backlog and Rotisserie intake limits;
- new intake of other issues pauses. Work that is already running continues,
  and other PRs still get review, CI repair and conflict work after the
  incident's work;
- an operator-forced run (`run --force`) and an explicit owner claim still
  work as usual. The fleet switch and the independent-review rule still apply.

When the issue closes (normally because its PR merged), intake resumes on the
next tick. The dispatcher then closes the incident's leftover attempt PRs
(`factory/<worker>-<N>-...` branches) as superseded and removes the label. It
never closes a PR under an owner claim (`factory:claimed`, `factory:override`)
or a PR a worker holds; while one exists the label stays, and a later tick
closes the PR once it is free. `incident stop` closes the free attempt PRs at
once, removes the label and clears the variable. On an issue that is still
open, `stop` ends the incident but keeps its PR. Until `stop` clears the
variable, the dispatcher reads an ended incident's issue once per tick.

`--attempts K` (1 to 6, default 3) is stored for racing: several providers
building separate fix attempts at once. Racing needs per-attempt leases and is
not live yet. Until it is, an incident gets one attempt.

The web UI has `incident_start` and `incident_stop` writes and an `incident`
read.
