# Changelog

All notable changes to Rotisserie will be documented in this file.

The project follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and [Semantic Versioning](https://semver.org/spec/v2.0.0.html). While the
public version is below `1.0.0`, minor releases may change unstable APIs; every
such change must be called out here and in its release notes.

## [Unreleased]

### Added

- Owner self-review exception: a person adds `factory:allow-self-review` to one
  pull request, and the model that authored its current head may review and
  approve that head. A label added by a bot or GitHub Actions does not count.
  The label is removed when the head changes or the PR closes. `baste
  allow-self-review pr:N` adds it, and `baste review pr:N` shows it.

- Factory code review is line-anchored. The reviewer prints one
  `FACTORY_REVIEW_FINDINGS_V1=` record: each finding names a path, a line or
  range on the head side of the PR diff, a severity (blocking or non-blocking)
  and the fix. The trusted controller drops and counts findings that are not
  inside a diff hunk of the exact head. Zero valid blocking findings approves;
  any blocking finding requests changes; a run without a parseable record fails
  and another model reviews. The reviewer's prose verdict no longer decides.
  The controller posts the valid findings as one PR review with inline comments
  anchored by path, line and side, checks each anchor, and resolves its own
  older threads that the new review replaces. Repair runs get the findings as
  their task, and the self-heal sweep resolves a factory finding thread once a
  later commit changes the cited lines. `baste review pr:N` shows the findings
  with file:line, and `baste recap` counts code reviews by result.
- `baste recap` lists, under "Needs you", merged factory PRs whose issue is
  still open, with the reason merge closure kept it open: the PR said it is only
  part of the issue, or named it only in its title. Both control rooms show
  the rows.
- An `auto` worker now weighs each eligible live-catalog model by its track
  record over the last 7 days, for the same action (implement, review,
  ci-repair, merge-conflict, review-findings) when it has 3 or more runs
  there. 20% of picks go to models with fewer than 3 runs, so new catalog
  models get tried. Picks among tried models use Thompson sampling, and no
  model takes more than 70% of them, so concurrent workers spread. With no
  usable history the pick is the previous uniform pick. The worker reads the
  published `factory-stats.json` once per run (no factory store reads). The
  run summary and `baste failures` show why the model was chosen, and
  `rotisserie factory stats` gains a "By action" column.
- `baste turning` says whether the factory is moving work: 🍗🔄 Turning,
  🐢 Slowing or 🔥🔥 Burning, with the most likely reason, the next command
  and the evidence (last worker start and outcome, dispatcher passes with their
  "none" answers, control workflow failures, capped providers, backlog limits,
  store state). `--follow` exits 10 on Burning and 11 on Slowing, with an
  optional macOS notification. `baste doctor` gains a `work turning` check,
  `baste recap` names a burning stretch, and both control rooms show the
  verdict at the top of the overview.
- The factory clears three stalls by itself on each dispatcher pass
  (`factory_self_heal.py sweep`). A PR whose repair verdict named no finding
  goes back to review once, and the next review avoids the model that wrote
  the verdict. A bot reviewer's thread that the factory answered "Fixed" with
  a commit in the current head is resolved; a thread a person started or joined
  is never touched. A `factory:merge-conflict` PR that GitHub can now merge
  gets main merged in, once per head, never while a worker or person holds it;
  a real conflict stays with merge-conflict work. Each action leaves a factory
  comment on the PR and an event in the factory store. `baste heal` lists them,
  `baste recap` has a "Fixed by itself" section, and both control rooms show a
  "Fixed by itself" panel. Stalls the factory clears no longer appear under
  "Needs you".
- `baste store migrate [--dry-run]` copies the factory's Neon state into the
  libSQL (Turso) store through the new `factory-store-migrate.yml` workflow
  (#257). The dry run writes nothing and reports the projection key count, the
  provider budget rows that hold a setting and every Neon table's row count.
  The real run copies leases, backoffs, health resets, events and the typed
  tables, never overwrites a stored row, counts every copied row back, and
  refuses to report success on a mismatch. The Diagnostics page has Migrate
  store (dry run) and Migrate store, and Read store run now picks the run kind
  from a list. Adopters re-run the installer to add the caller.
- `baste --progress` writes one JSON line per step to stderr. `baste unstick`
  reports each step: reading open work, checks and claims, the plan counts,
  and one line per action with its GitHub run link or its error. A failed
  request no longer hides the others; the JSON lists `started`, `failed` and
  `not_attempted`.
- The local control room runs every change, and the "See proposed actions"
  read, as a background job. "Unblock work" shows each step and the elapsed
  time while it runs, then what started (with run links), what was skipped and
  why, and any errors. A change is never stopped on a timeout. A reload finds
  the running job again. The hosted control room shows the operator run link
  as soon as GitHub accepts the request.
- The factory state export is encrypted (AES-256, PBKDF2) with the adopter's
  `FACTORY_EXPORT_KEY` secret before upload, because public repositories expose
  Actions artifacts. The workflow refuses to run without the key.
  `baste store export --out FILE` decrypts it locally with `FACTORY_EXPORT_KEY`
  set to the same key. Adopters re-run the installer to pass the secret.
- `baste` is the short name for the factory console: `baste status` runs
  `rotisserie factory status`, and `baste web` opens the local control room.
  Help and hints now name `baste`. `rotisserie factory ...` keeps working, and
  the original local-config commands stay under `rotisserie --config FILE`.
- Turso cost observability (#258, #253): `rotisserie factory db-usage`
  (published value, `--via github` or `--live`), a doctor line read from the
  published `factory-stats.json`, and a Database usage control in both control
  rooms. The status page job reads the Turso Platform API and publishes rows
  read and written, storage, sync, plan allowances, share used, days left, a
  month-end projection and a status (ok, warn, critical). `pause_new_claims`
  lets the Turso persistence code pause new claims at critical or on
  `BLOCKED`. Adopters re-run the installer to pass the `FACTORY_TURSO_API_TOKEN`
  and `FACTORY_TURSO_URL` secrets and add the `db-usage` label claim mode.
- libSQL (Turso) factory persistence, not yet in use (#256). `factory_libsql.py`
  talks to a libSQL server over the Hrana HTTP API, or to a SQLite file, with
  the standard library only. The `factory_turso_*` modules carry every ledger
  and operations function with the Neon names. Each write is one batch in one
  transaction, with its authorizing checks inside the statement; no
  transaction spans a GitHub call. `baste store selftest` runs the
  coordination test matrix against the hosted database through the new
  `factory-turso-selftest.yml` workflow, and `baste store export --out FILE` saves a
  portable SQLite backup through `factory-state-export.yml`. Both have
  controls on the Diagnostics page. Adopters re-run the installer to add the
  two callers. The cutover is #257.
- `rotisserie factory estimate issue:N --via github` counts attempts from exact
  Neon history (#238). The label claim workflow's new `estimate-history` mode
  runs one bounded, read-only query over `public.factory_state_events` and
  publishes per-item counts. An attempt is a worker run that reached a model;
  catalog refusals and runs without a working model are reported separately.
  One table maps outcome classes, and unknown classes count as attempts. The
  estimate falls back to GitHub markers when the run fails and names its
  source. `--no-wait` and `--run ID` split the wait. The web UI Item tracker
  gets Estimate and Exact estimate (Neon) buttons. Adopters re-run the
  installer to add the mode to their label claim caller.
- Incident mode: `rotisserie factory incident start issue:N [--attempts K]
  [--reason TEXT]`, `incident stop issue:N` and `incident` (list). The state
  lives in `FACTORY_INCIDENTS`, delivered in `FACTORY_REPO_VARS`, and the issue
  carries `factory:incident`. While the issue is open, its issue and PRs rank
  ahead of every other item for every worker, the issue skips intake limits,
  and new intake of other issues pauses. Running work is not cancelled. When the
  issue closes, intake resumes and the dispatcher closes leftover incident PRs
  as superseded. The web UI can start, stop and list incidents. Racing separate
  attempts is not live yet (#239).
- Slot leases in the Neon ledger (#239): `acquire_slot` leases one of several
  named slots (`attempt-K` or `piece-<id>`) on one target, with exactly one
  holder per slot under the repository advisory lock and distinct providers per
  target by default. The normal release and renewal cover slot rows. Nothing
  assigns slots yet; incident racing wires them in a later change.
- Fleet switch: `rotisserie factory providers disable|enable NAME` and
  `rotisserie factory models disable|enable PROVIDER MODEL`, each with
  `--reason`. The switch stores `FACTORY_DISABLED_PROVIDERS` and
  `FACTORY_DISABLED_MODELS` as repository variables, delivered in
  `FACTORY_REPO_VARS`, so the next dispatcher tick obeys it. Roster offers,
  worker assignment (forced runs too), model choice, failover, `unstick`, the
  local Ollama reviewer (`local-ollama`) and PR-Agent review skip a turned-off
  provider or model. A run that names it explicitly may still use it. Listings
  show what is off and why. The web UI can switch providers and models.
- `rotisserie factory checks main` (or `branch:NAME`): why the branch's
  product CI fails on its current commit, reading only the configured CI
  workflows, not factory jobs on the same commit. Job summaries now name
  TypeScript errors with file and line.
- Owner picks go first. An accepted claim adds `factory:override` to the item
  and its linked issue or PR, and a hand-off adds it to the evidence PR. The
  factory ranks those items ahead of every lane and priority (implement,
  review, repair and merge), and they bypass WIP and review backpressure
  (#212).
- `rotisserie factory history <target>`: an item's label changes, close and
  reopen events, factory markers and label-claim controller runs, in local
  time. `rotisserie factory labels --run <id>` shows an earlier label doctor
  report. `rotisserie factory use owner/name` saves the default factory
  repository, so `--repo` is optional. The web UI reads `history` too.
- `rotisserie factory claim`, `finish`, `runs` and `follow`, plus
  `wait-for-work --claim`, `labels --via github`, `leases --via github` and
  `why --via github`: label claims and the label doctor from the console without
  Neon. They replace the hand-built `gh workflow run` and polling chains used in
  the #191 rollout. GitHub errors now name the failed request. The web UI
  reaches the same commands (`runs`, `follow` read; `claim`, `finish`, `labels`
  start and return).

- GitHub label claims (#191). Agents with write access find work labeled
  `factory:available`, request it by adding `factory:claimed`, and get an
  accepted or rejected answer on the item (labels plus one status comment
  edited in place). The new `rotisserie-label-claim.yml` workflow takes the
  same Neon fenced lease as `reserve-next`; removing the label hands off on
  evidence or releases, then dispatches the factory's next step.
- `rotisserie factory labels`: a label doctor that audits claim and lifecycle
  labels against Neon and the controller frontier (dry run by default),
  repairs them with `--repair`, and deletes obsolete label definitions with
  `--repair --delete-legacy`.
- Outside agents can now claim `review`. The bridge records the reviewer's
  declared model and refuses a model that authored the PR, so the approval
  counts for the merge gate. This changes the #177 rule that outside agents
  never review.

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

### Removed

- The archived legacy factory snapshot under `reference/legacy-factory/`, its
  inventory (`MIGRATION_INVENTORY.md`), the inventory test and the 2026-10
  drift note. Git history keeps them; `MIGRATION.md` names the last commit
  that has them.
- `factory/scripts/opencode_pipeline.sh`, `run_opencode_issues.sh` and
  `issue-sweep-loop.sh`: one product's local pipelines and issue lists.
- `factory_work_policy_latticery_ref.py`, a retired extraction reference.
- `factory/docs/migration/comic-pile-*`: copy-time migration evidence that
  nothing reads. Git history keeps it.

### Changed

- The factory runs on its libSQL (Turso) store instead of Neon (#257).
  `factory_state_ledger.py` and `factory_state_operations.py` keep their
  public names and command lines and call the `factory_turso_*` modules. The
  Neon modules, the `factory-state-cache` action and the factory's asyncpg
  dependency are gone. Trusted jobs get the new `factory-store` action: the
  `FACTORY_TURSO_URL` secret and a 0600 token file (`FACTORY_TURSO_TOKEN`, or
  `FACTORY_TURSO_READ_TOKEN` for the read-only operator read and frontier
  workflows), removed by each job's last step. `FACTORY_STATE_REQUIRED`
  replaces `FACTORY_NEON_LEASE_REQUIRED` and `FACTORY_NEON_CONFIGURED`.
  Adopters must re-run the installer: until then, callers that still pass
  `NEON_API_KEY` fail closed.
- New factory claims pause while the published store usage is critical; an
  operator's explicit target, renewals and releases do not.
- The status job's retention step is `factory_state_operations.py prune`
  (30-day lifecycle events, 120-day raw model attempts). The reliability export
  runs at most hourly, and the dispatcher recovery watchdog runs hourly at :37.
- The `measure` dispatcher input reports store requests and rows read and
  written.

- Review independence excludes only the model that authored the current head
  (owner decision, 2026-11-24). A model that authored an earlier head may review
  once a later commit by someone else is the head; before, every model that
  authored any head was excluded, and long-lived PRs ran out of reviewers. A
  clean sync carries the author of the commit it merged into; a head with no
  author record excludes no model; a worker push without a model record, an
  unprovable merge or more than five syncs exclude every recorded author. The
  merge gate resolves the head author the same way as reviewer selection. `factory_review_exclusion.for_pull` is the
  shared resolver. `baste review` shows the head author, the excluded models
  and the earlier-head authors that may now review.
- Issues the factory never claims or closes come from `[issues] reserved` in
  `.rotisserie/config.toml` (default none) instead of a hard-coded list.
- The hosted dashboard's sign-in service comes from `[dashboard] auth_url`
  (or `build_hosted_control_room.py --auth-url`); without it the dashboard is
  read-only.
- `scripts/sync_factory_quota.py` reads quota observations from
  `GITHUB_REPOSITORY` and publishes to its owner's `OWNER.github.io`
  (`FACTORY_QUOTA_SOURCE_REPOSITORY` and `FACTORY_QUOTA_PUBLISH_REPOSITORY`
  override both). The quota observer's public alias is `factory-actions`.
- Labels, commit authors, PR titles and dashboards say "Factory N" and use
  the repository name instead of a product name.
- Factory comment markers derive their prefix from the adopter repository name
  (`factory_adopter.marker_prefix()`, mirrored in `factory-visibility.cjs` and
  the shell workers) instead of a hard-coded product prefix. `comic-pile`
  still produces its existing markers. `FACTORY_MARKER_PREFIX` overrides the
  prefix. `rotisserie factory` derives it from `--repo`.
- The local OpenCode factory scripts are `scripts/opencode-factory.sh`,
  `opencode-factory-heartbeat.sh` and `opencode-factory-overnight.sh` (were
  `comic-pile-opencode-factory*.sh`). Their `COMIC_PILE_*` variables are now
  `FACTORY_SOURCE_REPO`, `FACTORY_WORKTREE`, `FACTORY_STATE_DIR`,
  `FACTORY_DEFAULT_MODEL`, `FACTORY_AUTO_SCOUT`, `FACTORY_WAIT_FOR_SCOUT`,
  `FACTORY_SCOUT_READY_FILE`, `FACTORY_SCOUT_PID_FILE`,
  `FACTORY_ALLOWED_PROVIDERS` and `FACTORY_GOOGLE_FREE_MODELS`. The source
  repository defaults to the checkout that holds the scripts. The heartbeat
  takes its repository and marker prefix from `factory_adopter.py`.

### Fixed

- PRs the loop breaker parks or holds as looping no longer count toward the
  review backlog limit or the open-PR limit. No worker can drain them, so on
  2026-11-25 six parked PRs filled a limit of six and stopped all issue intake.
  The dispatcher, the Rotisserie intake decision, `baste why`, `baste unstick`
  and `baste turning` share one count. A separate cap still applies: once
  `FACTORY_PARKED_PR_LIMIT` parked PRs are open (default twice
  `FACTORY_REVIEW_BACKLOG_LIMIT`, set in the job environment), new issues wait,
  and `why`, `unstick` and `turning` say "N parked PRs waiting on the owner".

- Issue acceptance no longer rejects good reviews over parsing. Context
  sections such as "Existing implementation / related work" and "Original
  reporter evidence" are no longer criteria. A citation that joins files with a
  comma, a semicolon or "and" is split, and each file must be changed by the PR.
  A criterion that instructs verification in a deployed environment is marked
  `verify_after_merge`: it does not block approval, the approval comment lists
  it, and merge closure keeps the issue open with a checklist until its box is
  checked. The rejection note now gives the count and up to 20 reasons.
- The stranded-closure sweep has its own merge-drain step before the
  main-health gate. It skipped every drain tick while main was pending, so
  issues of PRs merged by a person waited for the next green tick.
- Post-merge closure prints one line per decision with the issue and the reason
  (for example "the PR says it is only part of #5751, so #5751 stays open"),
  not a bare `no-linked-issue`.
- The web UI now has a control for each command its server allowed but the
  page did not offer: Follow, Claim runs, History, Claim and Finish claim for
  one item, checks on `main`, the label doctor dry run and repair, and the
  provider and model switches. The models listing reports
  `disabled_reason`. The web server refuses to turn a provider or model off
  without a reason.
- An expired factory lease no longer blocks an outside claim on its item. The
  factory's own intake already treated those rows as free. An expired outside
  claim still blocks, because an elapsed timestamp never proves an outside
  agent stopped (#212).
- The post-merge closer no longer closes an issue a person reopened. Before,
  it closed adopter issue #5695 again because an earlier merged PR said "Closes",
  although the reproduction still failed. A reopen by the factory itself (for
  dependency blocks) still allows closure (#212).
- A claim on a named issue or PR (label claim or `rotisserie factory claim`)
  is no longer judged by factory intake. Capacity, backpressure, ranking and
  "offered now" checks applied, so adopter issue #5695, a reopened production bug,
  was refused while the review backlog was full. Only another active lease on
  the item now refuses a claim, and the refusal names the holder, the expiry
  and the command to end it (#212).
- Console commands that run a factory workflow (`labels --via github`,
  `leases --via github`, `claim` and the others on the bridge) now wait while a
  run created seconds ago still reads as missing. Before, they stopped at once
  with "the run's state could not be read" while the run went on (#191).
- Finishing a label claim now removes `factory:claimed` from every item the
  lease covered, not only the claimed item. Before, a PR claim left the label on
  the linked issue, which blocked that issue's wakeups (#191).
- The label doctor now ages a claim label by the item's last update when GitHub
  no longer lists the item's label events. Before, a stale `factory:claimed` on
  a busy issue was held forever as an unanswered request (#191).
- Factory merges are serialized, and each resulting main commit is verified
  before the next merge. The ready-merge drain merges at most one PR per run,
  and only onto a main commit whose product CI passed. An exclusive claim on
  main's exact SHA keeps the drain and the workers' canonical route from racing
  each other. The dispatcher no longer merges. After a merge, product CI is
  dispatched on the new main: `GITHUB_TOKEN` merges fire no push event, and a
  main with no CI run had been read as green. A red main admits only
  `main-breakage` PRs. New setting: `FACTORY_MAIN_CI_WORKFLOWS`. `doctor` gains a
  "main verification" check, and `doctor --fix` dispatches CI for an unverified
  main.

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
- `rotisserie factory`, the factory operator console (moved from the first adopter's
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
