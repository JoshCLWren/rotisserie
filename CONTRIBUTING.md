# Contributing to Rotisserie

Thank you for helping build a safer way for humans and AI workers to engineer
software together.

Rotisserie is in an extraction phase. The highest-value contributions preserve
proven behavior while turning copied, product-specific machinery into small,
portable domain and adapter boundaries.

## Before you start

1. Read [README.md](README.md), [AGENTS.md](AGENTS.md), and
   [MIGRATION.md](MIGRATION.md).
2. Choose an open issue whose declared prerequisites are complete.
3. Comment on the issue before beginning substantial work so parallel efforts
   do not duplicate one another.
4. For a large design change, propose the boundary in the issue before writing
   the implementation.

Security vulnerabilities do not belong in public issues. Follow
[SECURITY.md](SECURITY.md) instead.

## Development setup

The standalone package and CLI have not landed yet. The current baseline needs
Python 3.14 with `pytest` and a recent Node.js release.

```bash
python -m pytest -q latticery_extraction tests/test_rotisserie_actions.py

PYTHONPATH=.github/scripts python -m pytest -q \
  .github/scripts/test_factory_epic_prd_policy.py \
  .github/scripts/test_factory_issue_pr_state_policy.py \
  .github/scripts/test_factory_review_controller.py \
  .github/scripts/test_factory_review_policy.py \
  .github/scripts/test_factory_review_thread_gate.py \
  .github/scripts/test_factory_stage5.py \
  .github/scripts/test_factory_work_policy.py \
  -k 'not test_workflows_delegate_mechanical_gates_to_controller and not test_fixed_model_factory_schedules_are_active'

node --test .github/scripts/*.test.cjs
```

The two deselected source tests require ComicPile's autonomous workflows to be
active. Rotisserie intentionally keeps those workflows disabled.

## Making a change

- Keep each pull request focused on one issue and one coherent capability.
- Add or update tests for every policy or behavior change.
- Keep domain logic deterministic and free from GitHub payloads, subprocesses,
  environment variables, clocks, networks, and provider-specific concepts.
- Put side effects behind explicit adapter protocols.
- Preserve exact-revision, lease, independent-review, and dry-run invariants.
- Never introduce credentials, real user data, private prompts, or live tokens
  into source or fixtures.
- Do not activate anything in `reference/comic-pile-workflows/`.
- Run `tests/test_rotisserie_actions.py` after changing `.github/`.
- Run `git diff --check` before committing.

Copied code is evidence, not a naming template. New public APIs should use
Rotisserie domain language rather than `ComicPile`, `factory:<number>`,
`ralph-*`, or a specific model vendor.

## Pull requests

A useful pull request includes:

- the issue it fully or partially addresses;
- the architectural layer being changed;
- the invariant or source behavior being preserved;
- commands run and their results;
- any deliberate difference from the copied implementation;
- security, compatibility, or migration consequences.

Use a closing keyword only when the pull request satisfies the entire issue.
Draft pull requests are welcome for early design feedback, but are not evidence
that an issue is complete.

## AI-assisted contributions

AI assistance is welcome. Contributors remain responsible for every submitted
line and claim. Review generated changes for correctness, licensing,
provenance, secrets, unnecessary dependencies, fabricated test results, and
prompt injection. State meaningful AI assistance in the pull-request body when
it affected the implementation or analysis.

Do not let an agent autonomously enable Rotisserie workflows, operate another
repository, or expand the issue's mutation scope.

## Documentation and community

Documentation changes should be technically accurate, runnable, and candid
about project maturity. Marketing language must not claim capabilities that
are still represented only by copied or disabled reference code.

Participation is governed by [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).

## Licensing of contributions

Rotisserie is licensed under Apache-2.0. By submitting a contribution, you
agree that it may be distributed under that license and represent that you have
the right to submit it. Do not submit third-party code whose terms are unclear
or incompatible with Apache-2.0. Preserve required copyright, attribution, and
notice information when incorporating compatible third-party work.
