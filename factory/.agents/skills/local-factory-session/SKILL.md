---
name: local-factory-session
description: Bootstrap and hand off a local factory/issue session in Comic Pile so nothing needs manual reconfiguration. Use when starting local issue-driven work, before the first commit of a session, when commit provenance (Model trailer) or factory labels look wrong, when opening or reviving a factory PR, or when handing work to the scheduled review fleet.
metadata:
  opencode/slash: "true"
---

# Local factory session bootstrap and handoff

Everything a local session must set up once, and the exact state it must leave the
issue/PR in, so review fleet machinery can recognize and pick up the work.

The local factory owns authorized continuation through CI repair, actual
independent review, gated merge, deployment and real acceptance. A ready PR or
fleet handoff is not ordinary completion. In Codex, use
[the Crouton adapter](../crouton-factory-codex/SKILL.md) and
[the executable ACP review path](../../../docs/LOCAL_FACTORY_REVIEW.md) to start
eligible local reviewers instead of waiting for scheduled pickup. Record actual
review identity/model/session/head and `factory:review` + `factory:local` only
while local review runs; release to `factory:unowned` afterward. Never fabricate
controller identity or approval. Read ACP checkpoints/model-selection metadata
and final reports/verdicts only, never full logs. Existing authorization/gates
remain; remote factory backpressure does not constrain locals.

## Bootstrap (once per session, before the first commit)

1. Install hooks so commit tooling exists at all:

   ```bash
   bash scripts/install-git-hooks.sh
   ```

2. **Do not assume the hooks actually run.** Cursor (and similar hosts) remap
   `core.hooksPath` to a dispatcher directory that may be empty (e.g. `.git/hooks-disabled`).
   Check it:

   ```bash
   git config core.hooksPath
   ls "$(git rev-parse --git-common-dir)/hooks" | grep -v sample
   ```

   If `core.hooksPath` points at an empty directory, no hook fires — including the
   `prepare-commit-msg` hook that appends the producing-model trailer. Compensate manually
   (step 4).

3. Export the producing model for the session so any live hook can see it:

   ```bash
   export OPENCODE_MODEL=<your exact model id>
   # e.g. opencode/mimo-v2.6-flash-free for opencode sessions
   ```

4. After the **first commit**, verify provenance and amend if missing:

   ```bash
   git log -1 --format=%B | grep '^Model:' || echo "TRAILER MISSING"
   ```

   If missing, amend with the standard trailer block (the hook intentionally skips amends,
   so write it by hand):

   ```text
   <original subject and body>

   Model: <exact model id>
   Authoring session: interactive <host> session, no registered factory-worker identity
   ```

   Interactive factory commits always carry both lines. Scheduled factory workers carry only
   `Model: <roster id>`. Review machinery parses `^Model:\s*(\S+)$` for exact-head provenance;
   a commit without it makes the head unattributable.

## Verification gates (before every push)

- Python anywhere in the change: `bash scripts/check-python-ci-lint.sh` (whole-repo
  `ruff check .` + `ty check --error-on-warning`). Path-filtered runs are not valid.
- Frontend: run `pnpm run lint:anti-slop` from the repository root. From `frontend/`,
  run `pnpm run lint`, `pnpm run typecheck`, `pnpm run build`, and targeted Vitest files.
  Restore missing or stale worktree dependencies with `pnpm install --frozen-lockfile`
  before checking. Full suites and aggregate coverage run in CI, following the
  repository's targeted-local-test policy.
- Browser behavior: run the focused Playwright coverage; for work touching the e2e regression
  lane use the `e2e-chromium-regression` skill (P0 scope must be green).

## Issue and PR lifecycle

**Keep the local lease alive only while working.** The controller automatically
releases `factory:local` after 60 minutes without GitHub-recorded activity,
even when local tooling did not post a trusted claim marker. During longer
sessions, persist a concise, truthful issue/PR progress comment or update
within that window (ideally every 45 minutes). A local git commit or editor
activity that never reaches GitHub is not lease activity. Re-read the live
labels before pushing or updating them; if another worker now owns the target,
do not steal it back.

1. Claim with an atomic full label-set replacement (`gh api -X PUT .../issues/N/labels` with
   the complete set — never sequential add/remove): issue gets `ralph-status:in-progress`,
   `factory`, `factory:building`, one owner. Follow the `github-issue-kanban` skill and
   `docs/AUTONOMOUS_FACTORY_POLICY.md`.
2. Commit (imperative, component-scoped), push the branch, open a **ready-for-review** PR
   (never draft unless Josh asks). PR body states the verification evidence run.
3. Reconcile labels for review pickup **immediately after opening** — the scheduled fleet
   only claims `factory:unowned` work. Parking a review-waiting PR under `factory:local`
   means nobody picks it up:

   | Target | Labels |
   |---|---|
   | Issue | `ralph-status:in-review`, `factory`, `factory:review`, `factory:unowned` (+ priority/task/quality labels) |
   | PR | `factory`, `factory:review`, `factory:unowned` |

   Replace each set atomically via `gh api -X PUT repos/JoshCLWren/comic-pile/issues/N/labels`
   with every label in one call. POST only adds labels, leaving old owners and stages in place.

4. Every push to the PR head requires the same `factory:review` state to stay truthful
   (a push invalidates `factory:ci` / `factory:ready`).

## CI completion after every push

Opening a PR or handing it to the review fleet does not finish the implementation
turn. Keep monitoring CI for the exact pushed head until the required checks finish.
Use `gh pr checks <pr> --watch --interval 30`, keep progress updates flowing, and
re-read the head SHA before reporting the result. A later push invalidates the prior
CI result; monitor the new head instead. Confirm the CI workflow itself has
completed; green early bootstrap checks do not establish full CI success.

If a check fails, read its job log, reproduce the failing cases locally with targeted
tests, repair the cause, rerun the applicable local gates, commit and push. Repeat
until the required checks pass. Do not weaken tests, coverage, or lint rules. A skipped
coverage job caused by an upstream failure is not a passing coverage gate. While
repairing, claim the next-action lease; release it to review pickup after pushing as
specified above, and continue monitoring without reserving review ownership.

If an external condition prevents completion (for example unavailable credentials or
a service outage), record the exact blocker and current head in the durable resume
packet and tell the user which checks remain unresolved. Do not end ordinary work
with only "CI pending" or claim completion after local checks alone. CI success does
not grant merge authorization or replace exact-head review requirements.

## Stale-expiration is real

`comic-pile-factory-stale-expiration-v1` closes factory PRs idle ~24h
(`threshold-86400`) and releases the issue to `factory:unowned`. If that happens:

- GitHub **refuses to reopen** a PR whose branch was force-pushed after close
  (`state cannot be changed`); open a fresh PR from the same branch that supersedes the old
  one in the body, and say why it was closed.
- Rebase onto current `main`, re-run every gate, re-verify Playwright claims, then re-apply
  the pickup labels above.
- Leave a comment on the issue pointing at the new PR.

## Never

- Never edit `docs/changelog.md` / `docs/changelog.d/` for implementation PRs.
- Never enable auto-merge; never merge without every exact-head gate verified.
- Never leave an open factory PR without `factory`, one workflow-state label, and one owner
  label.
