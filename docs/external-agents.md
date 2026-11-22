# External agents: ranked work frontier (issue #177)

Rotisserie is the scheduling authority. GitHub issues and labels are useful
projections, **not permission** for an external agent to start a job.

## Inspecting the frontier

An installed Rotisserie CLI can display the same deterministic work-policy
candidate order as the factory runtime:

```sh
rotisserie factory next --repo JoshCLWren/comic-pile --json
```

Schema `1` includes the repository, 60-second observation expiry, ordered
candidate rank, exact target URL, action, instruction, policy lane/priority,
dependency hints, reported owner, exact PR head, known Neon target lease
details, capacity counters, reason, holds and provenance hints.

The result is **read-only**. The consumer must not treat `status=candidate`,
`rank=1`, `factory:unowned`, a lease expiry, or the absence of a GitHub
owner label as a successful reservation. `requires_claim=true` applies to
every response. If the CLI cannot inspect the live Neon ledger, all candidates
are held and `authoritative_claim_available=false`.

The list is a *snapshot* of the existing deterministic controller policy, not
an independent scheduler. A Jev decision made later, a new claim, a changed
PR head, an Actions job still alive after a lease timeout, or changed
backpressure can invalidate an earlier order. Distinct `parallel_group`
values identify target scopes, **not** proven conflict-free concurrent work.
Unknown file overlap, worker capacity and liveness are explicitly uncertain.

## Universal external-agent instruction

> Ask Rotisserie for the next meaningful work in the target repository.
> Read the ordered actions and their holds. Reserve the highest-ranked action
> you can perform **through the trusted controller**, never by editing labels
> or posting an issue comment. Revalidate the task/head, preserve the lease,
> do the action, report evidence, and release or hand off the claim. Let
> existing review, CI, merge and production gates decide completion.
> If a trusted claim interface is unavailable, report that missing access
> and **do not start changing code**.

## Remaining delivery under #177

The read-only `next` command is the initial upstream boundary, **not** full
external-agent support. It does not offer `claim`, `renew`, `release` or
`complete` yet. The remaining work is to implement those operations in the
trusted controller using fenced Neon transactions and current capacity rules,
install a GitHub Actions `workflow_dispatch` bridge for GitHub-only agents,
publish a bounded GitHub-visible queue, prove revocation and two-client race
behavior, and dogfood a real PR through independent review and deployment.

Until that work lands, `next` is a diagnostic/integration format, **never a
license to run external work**. No new active Rotisserie Actions workflows,
provider roster entries, or adopter mutations are part of this slice.
