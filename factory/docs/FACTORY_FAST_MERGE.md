# Factory-only fast merge

## Contract

Factory-only PRs merge automatically **after the exact PR head has passed Python
Ruff lint and ty type checking in the unprivileged CI workflow**. They do not
wait for the full CI summary or semantic reviewer. The factory-only path
allowlist, trusted-author rules, ownership/hold labels, head SHA pin, and branch
rules remain in force.

This bounded preflight was added after the November 17, 2026 incident: factory
status PRs #5329, #5332, #5333 and their repair #5334 were fast-merged before
CI, and failing Ruff code reached `main`. A check is not green just because
the workflow has started. Missing, pending, failed, wrong-head, untrusted-app,
or truncated check evidence **must skip the merge**.

## Execution

- [`factory-fast-merge.yml`](../.github/workflows/factory-fast-merge.yml)
  runs trusted `main` on PR open/reopen/synchronize/label and manual dispatch.
  A single job in `ci.yml` wakes it once both Python checks succeed. No
  scheduled polling, job-event storm, or approval phase is introduced.
- [`factory-fast-merge.py`](../.github/scripts/factory-fast-merge.py)
  reads candidate PR metadata and changed paths, then fetches GitHub check
  runs for the **exact** 40-character head SHA. It selects the latest genuine
  `github-actions` `Python Lint (ruff)` and `Python Type Check (ty)`
  results for that head. Both must be completed and successful.
- The merge is pinned to that head SHA with `merge_method=squash`. If another
  commit lands before the merge API call, the merge fails rather than accepting
  unchecked code.
- Ruff and ty execute in the ordinary **unprivileged** CI context, not in
  the privileged `pull_request_target` merge runner. The merge runner checks
  trusted GitHub results and **never executes PR code** with write credentials.
- Semantic review remains bypassed by this owner-authorized exception.
  Other required branch checks remain under GitHub's protection; the authorized
  merge token may have a narrowly scoped bypass, but it cannot bypass the
  controller's exact-head Python gates.

## Eligibility

The owner switch lives in
[`factory-fast-merge.json`](../.github/factory-fast-merge.json) and defaults
off if missing or malformed. Eligible PRs must be open, non-draft, mergeable,
same-repository, based on `main`, and authored by a trusted collaborator or
`github-actions[bot]`. Dependabot and Renovate are excluded.

The PR must carry `factory` and `factory:unowned` with no active factory
owner, `factory:blocked`, `needs-human`, `do-not-merge`, or
`security-review` label. Changed paths must pass the explicit factory-only
allowlist, including both sides of renames; unknown product paths are excluded.
Source-controlled provider/model roster files cannot be added or modified.

The fast-merge policy itself remains protected from self-approval:
`.github/factory-fast-merge.json`,
`.github/workflows/factory-fast-merge.yml`,
`.github/scripts/factory-fast-merge.py` and
`tests/test_factory_fast_merge.py` cannot use this exception.

## Operating instructions

1. Complete local focused checks and push a PR with an exact head SHA. Keep
   product code out of factory-only PRs.
2. The first fast-merge event may report `skipped`: `Exact-head Python Lint
   (ruff) has not passed` or `Exact-head Python Type Check (ty) has not passed`.
   This is intentional while CI is running, not a factory failure.
3. The `ci.yml` `factory-fast-merge-wakeup` job runs only after **both**
   Python CI jobs pass and dispatches one targeted, trusted evaluation.
   The controller re-verifies the exact head, then attempts the SHA-pinned
   merge without waiting for the full CI summary or semantic review.
4. A green fast-merge workflow does not imply a merge. Read `merged` and
   `skipped` in its JSON output. A red check blocks the merge until code is
   repaired and a new head passes.
5. Verify the PR actually merged, the linked issue was reconciled, and the
   production deployment was dispatched. A merged PR does not prove a healthy
   production deployment.

A manual targeted re-evaluation remains available:

```bash
gh workflow run factory-fast-merge.yml --ref main -f pr=1234
```

This does not bypass failing checks; replace `1234` with the PR number.

## Recovery and security

| Outcome | Operator response |
| --- | --- |
| `enabled: false` | Update owner switch through the protected path |
| Missing/failed/pending Python checks | Correct CI failures; the next successful exact-head check will retrigger evaluation |
| Missing labels, active lease, hold, or outside allowlist | Correct genuine ownership or scope issues, not the safeguards |
| Branch protection rejects merge | Fix the scoped credential authorization; never fabricate or forge checks |
| `merged` contains PR number | Verify actual merge, production dispatch, and post-merge closure |
| Workflow successful but `merged: []` | Read `skipped`; successful evaluation is not delivered code |

Changes to protected fast-merge policy require the normal reviewed and
CI-verified path. See
[`AUTONOMOUS_FACTORY_POLICY.md`](AUTONOMOUS_FACTORY_POLICY.md).
