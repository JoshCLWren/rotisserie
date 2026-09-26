## Issue

Link the issue and state whether this pull request fully closes it.

## Change

Describe the capability delivered and the architectural layer changed.

## Preserved invariants

- [ ] Deterministic graph/policy behavior remains deterministic.
- [ ] Repository and credential authority remains explicitly bounded.
- [ ] Exact-revision evidence cannot authorize a changed head.
- [ ] Dry-run paths perform no remote mutation.
- [ ] No copied ComicPile workflow was activated.

Remove invariant items that truly do not apply and explain why.

## Verification

List exact commands and results. Do not claim checks that were not run.

## Migration and security notes

Document deliberate differences from the source Factory, compatibility impact,
new permissions, new dependencies, untrusted inputs, and rollback behavior.

## AI assistance

Disclose meaningful AI assistance and confirm that a human contributor reviewed
the submitted code, tests, provenance, and claims.
