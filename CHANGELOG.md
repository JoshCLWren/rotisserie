# Changelog

All notable changes to Rotisserie will be documented in this file.

The project follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and [Semantic Versioning](https://semver.org/spec/v2.0.0.html). While the
public version is below `1.0.0`, minor releases may change unstable APIs; every
such change must be called out here and in its release notes.

## [Unreleased]

### Added

- `rotisserie factory sync` and `rotisserie factory leases`: Rotisserie now owns
  branch synchronization. A sync takes a short Neon operation lease on the PR's
  target key, so workers can't be assigned it mid-sync and a held PR is never
  touched. The write is a compare-and-swap update-branch, and conflicts go to
  merge-conflict work. Without Neon, the console queues requests
  (`factory-sync-main` label) for the dispatcher's new leased sync pass, or
  `--now` syncs on owner labels and says so. `unstick --yes` and `doctor --fix`
  use the same implementation. The ledger gains `acquire_operation_lease`,
  `release_operation_lease`, `target_lease`, `list_leases` and
  `release_expired_leases`. Replaces the retired `sync_watch.py` watcher.

### Fixed

- `run --take-over` could lose the item to a routine scheduler pass between
  stopping the holder and its own request landing ("busy"). It now waits for
  the scheduler's decision and takes over once more if that happened.
- `outcomes` judged ci-repair by the PR's stage label; it now requires the
  PR's checks to pass. When a worker fails validation it names the factory
  gate that rejected the change (e.g. "factory fast checks failed: native
  ingress/deployment/write guard tests"), which was otherwise only in /tmp on
  the runner.
- `report` counted a worker holding an issue and its PR twice, so it
  disagreed with `status` (e.g. 12 vs 6). Both now count one entry per worker.
- `outcomes --all` could not read routine scheduler passes ("finished without
  recording a decision"). It now lists every worker each pass started, marks
  cancelled passes, and counts a PR someone closed as overtaken, not missed.
- `unstick` picked models the provider no longer offers, so every worker it
  started failed at model selection ("not a current zero-price tool-capable Go
  model"). It now checks the live model lists (local OpenCode CLI, published
  catalogs) and skips dropped models; models it cannot check rank last and say
  so. `models PROVIDER` marks track-record models the provider dropped.
- `stats` showed models as paused after their cooldown had ended.

### Added

- `rotisserie factory conflicts pr:N`: the PR's files that main also changed
  since its merge base, and the merged PR behind each. `unstick` shows the
  overlap on merge-conflict tasks and hands an item to a person after three
  models fail its conflicts.
- `doctor --fix` re-runs the stats publisher when the snapshot is missing or
  stale (GitHub's schedule lagged by over an hour tonight).
- `rotisserie factory doctor`: reliability snapshot missing or stale, the
  dashboard publisher's last failure (step and error), scheduler stopped,
  intake undecidable, and one factory gate rejecting most workers.
- `unstick --skip ITEM` and `--only ITEM` scope a plan to what the operator
  wants; runs plan again from live state, so `--only` starts exactly what a
  dry run showed.
- `rotisserie factory checks pr:N` summarizes each failing check on a PR's
  head (formatting diff with files, failing tests, compile errors). `unstick`
  sends ci-repair instead of review work to PRs with red checks and says when
  a held PR needs a ci-repair takeover.
- `outcomes --by-item` (rounds, misses and latest result per item; flags items
  going in circles) and `outcomes --limit N`.
- `rotisserie factory close pr:N --reason TEXT` stops any worker on the PR,
  closes it with the reason, and closes its issue as not planned.
- `unstick` starts at most as many issues as the review-backlog and open-PR
  limits leave room for, so clearing a jam does not immediately recreate it.
- `unstick` recognizes probably-obsolete backlog PRs (`main` deleted most of
  the files they change) and lists them for a person to close instead of
  sending merge-conflict workers that cannot succeed.
- `rotisserie factory outcomes` follows each requested piece of work to its
  effect and says whether its goal holds now (conflict gone, checks fixed,
  review decided, PR delivered), flagging workers that reported success or
  "nothing to do" while the problem remains. `unstick` uses it to avoid
  sending a model back to an item it just missed on, so workers spread across
  models.
- `status` shows each worker's model. Worker runs are now named
  `Factory N · MODEL · fixed-model entry` (`auto` when the worker picks it);
  every run-name parser accepts both that and the older model-less name.
- `rotisserie factory unstick` plans and starts the work that clears whatever
  holds the factory back. For a full review backlog that means reviewing and
  repairing the backlog PRs no worker holds. Each worker uses the provider and
  model with the best delivered track record. It confirms before starting and
  supports `--dry-run`, `--max N` and `--yes`.
- `rotisserie.factory_console.service`: a front-end-neutral layer (queue
  eligibility, unstick planning, dispatch, reliability, provider setup) with
  typed results and structured reasons, so a TUI, web GUI or MCP server can
  reuse the console's logic. `run`, `stats` and `providers` now use it.
- `rotisserie factory why [issue:N]` explains why the factory is or is not
  picking up an issue: blocked or manual-only labels, open dependencies, an
  existing pull request, a worker already on it, Rotisserie intake, or the
  open-PR and review-backlog limits (with the live numbers and the setting to
  change). With no argument it summarizes every open issue. Answers come from
  the dispatcher's own rules: the controller's `explain` core
  (`explain_issue_report`) and Rotisserie intake decision (`intake_verdicts`)
  now live in shared modules the console ships. A run that assigns no work
  now points at `why`.
- `rotisserie factory`, the factory operator console (moved from ComicPile's
  `scripts/factory-run`): `run`, `watch`, `status`, `cancel`, `report`,
  `stats`, `settings`, `prioritize`, `providers` and `models`. Progress is
  read from real GitHub signals in plain language and distinguishes
  assignment from a model actually working. It has `--json` output, stable
  exit codes and Ctrl+C that never cancels work. Provider, worker and settings
  metadata come from the packaged factory runtime; no lists are duplicated.
  `--config` is now required only for graph commands. See
  `docs/factory-console.md`.
- Operator force always yields work: it ignores every limit the factory sets
  for itself, uses extra worker identities beyond configured capacity, and can
  target blocked items. It never takes an item another worker holds and never
  switches provider or model.
- Factory settings registry (`factory_settings.py`) delivered to workflows as
  repository variables, a configurable priority order, and a published
  provider/model reliability snapshot (`factory-stats.json`).

- Bounded exact-head completion claim and guarded merge handoff with complete
  trusted contributor provenance, semantic review, CI/readiness evidence,
  durable full-view CAS and an actual expected-head merge fence. Versioned
  completion records retain uncertain outcomes and suppress duplicate attempts;
  no compensating release or post-merge authority is implied. The public
  `controller-completion-plan` CLI and fixture caller perform planning only.
  Production enforcement and live adopter parity/cutover remain follow-on work.

- Bounded exact-head independent-review claim/request and process handoff over
  explicit acquisition, trusted provenance/liveness, durable CAS and dispatch
  ports. Contributor-set independence, exact durable bindings, concurrent-state
  fences and proven-refusal-only compensation guard mutation authority. The
  version-1 `controller-review-plan` CLI and fixture caller perform planning
  only; production enforcement and live adopter cutover remain open.

- Opt-in operational worker execution through `execute`, a strict version 1
  assignment/process JSON contract, and a trusted POSIX subprocess adapter.
  Active-lease, repository, inventory, human-boundary and exact-head checks
  guard execution; shared deadlines, cancellation, heartbeat records, local
  duplicate locks and durable receipts bound each attempt. Only unavailable
  or rate-limited results fall back. Dry-run starts no subprocess and the
  separate `[execution].enabled` gate is disabled by default. This activates
  no workflow, automatic dispatch, remote mutation or live adopter cutover.
- Portable, host-neutral per-node queue-eligibility rules (`evaluate_eligibility`,
  `EligibilitySignals`, `EligibilityVerdict`) and the exclusion codes they emit.
  `implementation_decision` delegates to this engine for one canonical
  `WorkBlock` ordering and verdict shape, with label and marker encoding left to
  the adopter translator.
- Config-derived worker inventory via an optional `[workers]` TOML table
  declaring `expected`, `retired`, and `scheduled` worker identities. `plan`
  and `dogfood` derive their work-in-progress ceiling from the `expected` size
  and report the resolved `wip_limit`. The `--max-active` flag is now an
  explicit override (canonically spelled `--wip-limit`) whose default changed
  from a fixed `1` to the configured inventory size, falling back to `1` only
  when no `[workers]` table is present.
- Provider-neutral dispatcher zero-work short-circuit: a single-read
  `dispatch_plan` tick that reports a trustworthy `NO_ELIGIBLE_WORK` status
  versus a capacity-bounded `WORK_AVAILABLE`, distinguishing genuine idle from
  WIP-capacity or intake-backpressure holds, surfaced in the `plan` CLI
  envelope via a new `dispatch` field and `--active-changes` flag.
- Portable exact-head review-independence rule in the domain: a reviewer must be
  absent from the exact-head contributor set (recorded lineage unioned with the
  attested producer). A single independent reviewer authorizes the head only
  when provenance is recorded and the producer is attested by exact-head
  evidence; an unattested or unknown producer, or unrecorded provenance, fails
  closed to two distinct eligible reviewers, each carrying a distinct stable
  block code.
- Read-only GitHub review-evidence adapter that projects trusted review-thread
  comments into those signals. Both contributor and semantic-review markers are
  honored only as whole-body comments; identities are normalized once (strip and
  casefold) so exclusion is case-insensitive and the reserved ``unknown``
  sentinel cannot be forged in any casing on any field; the attested producer is
  authoritative over any caller-supplied value; and the trusted-author set is
  empty by default so an adopter must opt its controller identity in. The
  adapter performs no mutation.
- Versioned, idempotent adopter transition commands that bind an authorized
  decision to an exact control revision, bounded lane, and compare-and-swap
  stage change without carrying credentials or host mutation behavior.
- Versioned adopter rollback-drill and canary-observation evidence bound to an
  exact lane, stage transition, and adopter-owned control revision.
- Atomic, read-only `shadow-project` CLI projection and comparison with exact
  baseline and graph digests, repository scoping, and no intermediate
  Rotisserie decision file.
- Schema-version-2 adoption decisions with explicit current and next stages,
  fail-closed transition ordering, and unconditional active-lane rollback to
  the legacy stage.
- Read-only `decide` CLI projection from versioned graph snapshots into all six
  adopter-shadow dimensions, with explicit empty-dimension coverage, input
  digests, repository scoping, and no mutable graph-state initialization.
- Non-mutating `adopt` CLI decisions over validated shadow reports, including
  bounded lanes, explicit approval gates, and unconditional rollback requests.
- Fail-closed external-adopter cutover and rollback decisions with bounded
  lanes, complete shadow-evidence checks, and explicit operator approval.
- Versioned external-adopter decision snapshots and deterministic shadow
  reports across eligibility, ranking, ownership, review, completion, and
  recovery, with exact-graph-revision enforcement.
- Read-only `shadow` CLI comparison with input digests, durable reports, and a
  distinct exit status for explained adopter divergence.
- Guarded dogfood payload streaming over standard input, allowing live
  read-only projection without retaining raw GitHub responses.
- Non-activating self-dogfood commands for fixture projection, read-only
  decisions, credential-free GitHub mutation plans, and durable evidence.
- Standalone Python package, locked development environment, and canonical
  verification commands.
- Explicit migration inventory for every archived source artifact.
- Provider-neutral graph identities, entities, relationships, validation, and
  versioned snapshot serialization under the pure domain boundary.
- Bounded provider-neutral worker assignments, authority manifests, attempt
  events, exact-revision evidence, secret-free resume packets, and deterministic
  executor fallback contracts.
- Versioned, repository-allowlisted local operator configuration and a
  credential-free fixture simulation adapter with atomic durable state.
- JSON CLI commands for graph inspection, planning, claim, bounded run, review,
  completion, recovery, and diagnostics; mutations are dry-run by default.
- Schema-versioned structured operation records, correlation IDs, derived
  metrics, and recursively redacted diagnostic bundles.

## Release notes

Each release entry must describe added, changed, deprecated, removed, fixed,
and security-relevant behavior where applicable. GitHub release notes should
link to the matching changelog entry, state supported Python versions, and
identify any migration or compatibility action an adopter must take.

[Unreleased]: https://github.com/JoshCLWren/rotisserie/compare/v0.1.0...HEAD
