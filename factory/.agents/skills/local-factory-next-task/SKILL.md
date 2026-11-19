---
name: local-factory-next-task
description: Find the oldest factory-claimable task without interfering with the scheduled GitHub Action factories, and start it as a local factory session. Use when the user says find the next task, pick the next issue, what should we work on, start factory work, claim the oldest claimable task, take contention-free or tail work from the backlog, or work as a local factory in this repository.
metadata:
  opencode/slash: "true"
---

# Start the oldest claimable task as a local factory

This is the startup half of local factory work; `local-factory-session` covers session
bootstrap, commit provenance, and the review handoff. `github-issue-kanban` remains the
general issue-workflow reference. The canonical copy-paste master prompt is
`prompts/agent-next-task.md`.

## 1. Bootstrap the session first

Follow the `local-factory-session` skill before the first commit: run
`bash scripts/install-git-hooks.sh`, verify whether hooks actually fire (inert
`core.hooksPath`), export `OPENCODE_MODEL`, and plan to verify the `Model:` trailer after
commit #1.

Also: inspect `git status` and preserve any existing user changes.

### Claim a private worktree

Never share a checkout with another agent (Codex, Cursor, other opencode sessions).
Parallel agents in one worktree clobber branches, build artifacts, and ports. One session =
one worktree, following the existing on-disk convention (`comic-pile-<issue-or-topic>`
under `/mnt/extra/josh/code/` or `/home/josh/tmp/`):

```bash
git -C /mnt/extra/josh/code/comic-pile-<main-or-topic> worktree add \
  /mnt/extra/josh/code/comic-pile-<issue> -b <branch-name> origin/main
```

Run every subsequent command from that worktree. `make worktrees` lists existing ones; if a
worktree for your issue already exists, enter it instead of creating another.

### Claim a port slot

Separate worktrees isolate compose *projects* (project name derives from the directory), but
**host-published ports are machine-global** — two sessions on defaults will collide on the
test Postgres (5437), api-test (8002), redis-test (6379), the e2e backend (9000), and the
dev API (8000). Claim the smallest free slot N in 1–9 for the whole session
(`ss -ltn | grep -E ':(800[0-9]|900[0-9]|543[7-9])'`), then export:

| Resource | Default | Slot N |
|---|---|---|
| Dev API (`PORT`, Makefile convention: main=8000, worktrees 8001+) | 8000 | `PORT=$((8000+N))` |
| E2E backend / Playwright (`BASE_URL`) | 9000 | `BASE_URL=http://127.0.0.1:$((9000+N))` |
| Test Postgres publish (`E2E_POSTGRES_PUBLISH`) | 5437 | `E2E_POSTGRES_PUBLISH=$((5437+N)):5432` |
| api-test publish (`E2E_API_PORT`, only if using the full compose stack) | 8002 | `E2E_API_PORT=$((8002+N))` |
| redis-test publish (`E2E_REDIS_PUBLISH`, only for full compose stack) | 6379 | `E2E_REDIS_PUBLISH=$((6379+N))` |

The e2e lane needs only Postgres plus a locally started uvicorn — bring up the single
service (`docker compose -f docker-compose.test.yml up -d postgres-test` with
`E2E_POSTGRES_PUBLISH` set) to skip the redis/api-test ports entirely. Details in the
`e2e-chromium-regression` skill.

## 2. Read durable context

- `AGENTS.md`
- `docs/ISSUE_EXECUTION_PROTOCOL.md`
- `docs/AUTONOMOUS_FACTORY_POLICY.md` (factory work — claim leases, review, merge gates)

## 3. Select the task without interfering with the Action factories

```bash
make next-task
```

Work on the selected issue unless the user names another. Selection semantics
(`scripts/next_task.py`):

- **Claimable** means: `ralph-status:pending` + `ralph-task`, and none of `duplicate`,
  `ralph-status:{blocked,done,in-progress,in-review}`; not `epic`/`prd`; body has no
  `<!-- factory-execution:manual-only -->` marker; not an acceptance-parent contract; and no
  declared dependency issue left open.
- **Ordering**: highest priority first (`critical` → `high` → `medium` → `low` → unprioritized),
  tie-broken by **lowest issue number — i.e. the oldest claimable task wins within a
  priority band**.

**Local/Ralph selection is different from scheduled fixed-model dispatch.**
`scripts/next_task.py` checks the Ralph pending queue; the scheduled factories use
`.github/scripts/factory-work-controller.py` and additionally enforce Rotisserie
intake authorization, rolling no-diff retry limits, PR ownership, backpressure,
and worker-specific limits. An issue can be locally Ralph-eligible while remote
dispatch is blocked. A missing local `rotisserie` executable or exhausted
*remote* no-diff budget is not by itself a veto on the local start workflow.
The local session must still honor current ownership, open PRs, dependencies,
and the Rust cutover rule. Never overwrite another worker's claim.**

Before claiming, verify the candidate against **live** GitHub state:

1. **Owner and stage labels**: re-read the issue labels. An active
   `factory:local` or `factory:<n>` owner is a real lease: skip. Only proceed
   with new implementation when the issue has no active owner, is pending,
   and has no in-flight workflow stage. `factory:review`, `factory:ci`,
   `factory:changes-requested`, `factory:ready` and similar stages can
   denote an existing PR handoff even when paired with `factory:unowned`:
   do not restart implementation against them.
2. **Existing canonical PR**: inspect any open PR linked to the issue
   (closing reference or canonical branch), not just a text search hit.
   If one owns the implementation, advance that PR instead of starting
   another issue branch.
3. **Comments are audit history, not leases**: old controller claim/release
   comments and `factory-resume:v1` packets never independently reserve an
   issue. Do not require the latest historical marker to be a release when
   current labels are unowned. If comments and current labels appear to
   conflict, re-read the live issue/PR state and use the controller's
   reconciliation path for genuinely contradictory or stale leases; do
   not forge a release comment or overwrite another owner's labels.

These checks avoid duplicate work without importing the remote-only
Rotisserie and retry gates into a local factory start.
If the top candidate is contended, walk the ordered candidate list (same claimability +
ordering rules, already excluding factory-owned issues) and take the first uncontended one.
When the user asks for the *oldest* claimable work specifically (the low-contention tail the
factories drain last), pick the oldest (lowest-number) uncontended candidate from this list
rather than the head:

```bash
gh issue list --state open --limit 200 --json number,title,labels --jq '
  def prio: ([.labels[].name | select(startswith("ralph-priority:"))
    | {"ralph-priority:critical":0,"ralph-priority:high":1,"ralph-priority:medium":2,"ralph-priority:low":3}[.] // 99] | min) // 99;
  [ .[]
    | select(any(.labels[].name; . == "ralph-status:pending") and any(.labels[].name; . == "ralph-task"))
    | select(all(.labels[].name; IN("duplicate","ralph-status:blocked","ralph-status:done","ralph-status:in-progress","ralph-status:in-review") | not))
    | select([.labels[].name | select(startswith("factory:")) | select(. != "factory:unowned")] | length == 0)
  ] | sort_by(prio, .number) | .[] | "p\(prio)  #\(.number)  \(.title[0:70])"'
```

Then read the issue body, any linked plan (`docs/issue-plans/<number>.md`, bounded chunks),
and the named files. If the issue is too large for one implementation pass, split it into
linked task/subtask issues before editing code. If required work turns out to be outside the
issue, create a linked issue instead of widening scope.

## 4. Claim it (two steps — status first, then the atomic factory set)

1. Validate and flip status:

   ```bash
   make start-task ISSUE=<number>
   ```

   This refuses non-pending/non-executable/manual-only/dependency-blocked issues, moves
   `ralph-status:pending` → `ralph-status:in-progress`, and posts the starting-implementation
   comment.

2. Reconcile the **complete** label set in one atomic write (never sequential add/remove), so
   the issue immediately carries truthful factory state for a local session:

   ```bash
   gh api -X PUT repos/JoshCLWren/comic-pile/issues/<number>/labels \
     -F "labels[]=ralph-task" -F "labels[]=ralph-status:in-progress" \
     -F "labels[]=ralph-priority:<band>" \
     -F "labels[]=factory" -F "labels[]=factory:building" -F "labels[]=factory:local" \
     -F "labels[]=<preserve every other unrelated label from the issue>"
   ```

   Target set: the issue's existing unrelated labels (priority, `quality-control`,
   `infrastructure`, etc.) + `ralph-status:in-progress` + `factory` + `factory:building` +
   exactly one owner (`factory:local` for a local session). **Re-read** the live labels
   immediately after `make start-task` and before PUT. If another worker has
   gained ownership, do not overwrite it. Preserve all unrelated labels.

   Use PUT for replacement: POST only adds labels and retains old owners, stages, and statuses.

**Claim immediately after picking**, before implementation. If `make start-task`
fails with "not pending", re-read live labels and select the next candidate.
The status transition is not the complete factory lease; finish the atomic
`factory:local` label update promptly. If a foreign owner appears before or
during your work, stop and preserve your changes without clearing or
overwriting their lease. The controller reconciles stale/contradictory leases;
historical comments alone are not grounds for taking or releasing ownership.

## 5. Work the issue

- Implement only that issue's scope; add regression tests for changed behavior.
- Fix every failure; never skip tests, never use CI as a local debugger, never suppress.
- Python changes: `bash scripts/check-python-ci-lint.sh` (whole-repo ruff + ty) after the
  last edit and before every push. Frontend changes: `pnpm run lint && pnpm run typecheck &&
  pnpm test && pnpm run build`. Browser behavior: see the relevant e2e skill.
- If blocked: set `ralph-status:blocked` with the exact blocker and stop.
- If ending a turn with work unfinished, update the canonical `<!-- factory-resume:v1 -->`
  comment (head, hypothesis, files touched, checks, next narrow verification, remaining
  action, worker id, UTC timestamp).

## 6. Finish through the handoff

When implementation and gates are done, follow `local-factory-session`: commit with the
`Model:` trailer, push, open a ready-for-review PR linked to the issue, post the
verification comment (files, acceptance evidence, test commands + results), and reconcile
the issue/PR to the fleet pickup state — `ralph-status:in-review` + `factory:review` +
`factory:unowned` (issue) and `factory` + `factory:review` + `factory:unowned` (PR).
Leave the issue open until the PR merges and acceptance criteria are verified.
