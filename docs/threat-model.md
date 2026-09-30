# Threat model and security boundary review

This review covers the current extraction and local/adopter CLI boundary for
issue #11. It is not a claim that general autonomous execution is production
ready. Assets are repository integrity, credentials, exact-revision evidence,
exclusive leases, durable recovery state, and operator approval.

Untrusted inputs include issue text, pull-request content, fixtures, worker
output, resume packets, host payloads, and stale or replayed evidence. Trusted
inputs are explicit operator configuration, repository allowlists, authority
manifests, and the adapter's authenticated host identity. A local operator with
write access to configuration and state can change both; local files are not an
authenticated security boundary against that operator.

| Threat | Current control | Regression evidence |
|---|---|---|
| Cross-repository mutation or missing identity | Repository and target allowlists; credential identity checked at effect boundary | `test_github_mutations.py`, `test_github_projection.py` |
| Stale review or changed head | Evidence and change effects bind exact revisions | `test_domain_policy.py`, `test_application_coordination.py`, `test_github_canary.py` |
| Duplicate work or racing leases | Deterministic suppression, expiry, compare-and-swap and idempotency keys | `test_domain_policy.py`, `test_application_coordination.py` |
| Producer reviewing its own work | Separate durable worker identities | `test_worker_runtime.py`, `test_domain_policy.py` |
| Prompt content becoming authority | Trusted assignment separated from untrusted input; bounded results and resume validation | `test_worker_runtime.py` |
| Secret persistence | Credential isolation, durable runtime validation and diagnostic redaction | `test_worker_runtime.py`, `test_operator_cli.py` |
| Dry-run using remote credentials or transport | Credential-free planning, fail-fast sentinels | `test_github_mutations.py`, `test_operator_cli.py` |
| Replayed cutover authorization | Lane, stage, control revision and observed evidence binding | `test_application_adoption.py` |
| Imported automation or privileged PR execution | Archived workflows inert; only CI and bounded manual canary active | `test_rotisserie_actions.py`, `test_github_canary.py` |

## Residual risks and deployment assumptions

Redaction recognizes credential-shaped data; it is not a general secret detector.
Never supply real credentials as fixture, graph, CLI argument, prompt, or worker
state. A compromised adapter or host can fabricate evidence; callers must
protect authenticated acquisition and enforce compare-and-swap themselves.
Semantic review depends on the integrity of the configured worker identity and
evidence source. Local persistence is intended for simulation, not concurrent
multi-host coordination. Provider subprocess isolation and general remote
worker execution are not shipped entrypoint capabilities.

An adopter owns enforcement of authorized transition commands and its rollback
switch. Pure decisions do not themselves mutate a host. The completed merge
canary remains manually dispatched, repository-scoped, issue-9-only, and
unscheduled. Expanding workflows, credentials, mutation lanes, or worker
execution requires a separately approved boundary change and tests.

## Release review still required

Before a supported release, review dependency and build inputs, run secret
scanning and static security analysis, generate an SBOM, and verify provenance
and checksums against tagged source. Record tool versions and findings, including
unresolved findings. Those supply-chain checks are pending; passing unit tests
alone does not satisfy the full issue #11 release contract.
