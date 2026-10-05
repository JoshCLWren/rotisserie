# Queue eligibility rules

Rotisserie owns the portable rules that decide whether one work node may enter
autonomous implementation. The rules live in the domain layer
(`rotisserie.domain.eligibility`) and operate only on pre-decoded, host-neutral
signals. They never parse GitHub payloads, labels, HTML markers, issue bodies,
or any other product encoding — that translation is an adopter concern.

This boundary ports the shared semantics ComicPile keeps in
`scripts/factory_eligibility.py` and documents in its
`docs/FACTORY_QUEUE_ELIGIBILITY.md`, splitting them along Rotisserie's
architecture: the deterministic rules move into the domain, while the label and
marker *encoding* stays in the adopter's translator layer.

## Domain contract

`EligibilitySignals` is the decoded input for one work node. Every field is
already translated out of its host or product vocabulary:

| Signal | Meaning |
|---|---|
| `state` | Decoded lifecycle state; only `OPEN` work is executable. |
| `product_excluded` | Adopter-declared product exclusion (registry, watchdog, protected node). |
| `parent` | Parent or aggregation node (epic/PRD or declared acceptance parent), not an executable leaf. |
| `human_gate` | A human-only boundary is present; autonomous execution is gated. |
| `blocked` | A blocking signal is present. |
| `terminal` | A terminal signal is present. |
| `active_lease` | Another active lease already owns the node. |
| `unresolved_dependencies` | Declared prerequisites not yet satisfied (self-reference ignored). |
| `implementation_exists` | An existing implementation already covers the node. |
| `retry_budget_exhausted` | A bounded retry budget is spent; the node waits for new evidence. |
| `missing_selector_metadata` | Selector-scoped metadata tokens a specific selector requires but the node lacks. |

`evaluate_eligibility(signals)` returns an `EligibilityVerdict` carrying the
stable `WorkBlock` exclusion codes in canonical evaluation order plus parallel
human-readable reasons. A node is eligible only when no exclusion applies.

The evaluation is pure, deterministic, and fails closed: `state` must be
supplied explicitly (there is no implicit "assumed open"), every applicable
exclusion is reported rather than only the first, and the sole eligible outcome
is the absence of any exclusion.

## Evaluation order

The order matches ComicPile's static exclusion list so verdicts remain
comparable across the shadow boundary:

1. Not open — `WorkBlock.NOT_OPEN`
2. Product exclusion — `WorkBlock.PRODUCT_EXCLUSION`
3. Parent / aggregation node — `WorkBlock.PARENT_WORK`
4. Human-only boundary — `WorkBlock.HUMAN_BOUNDARY`
5. Blocked — `WorkBlock.BLOCKED`
6. Terminal — `WorkBlock.TERMINAL`
7. Active lease — `WorkBlock.ACTIVE_LEASE`
8. Unresolved dependencies — `WorkBlock.DEPENDENCY_INCOMPLETE`
9. Existing implementation — `WorkBlock.IMPLEMENTATION_EXISTS`
10. Retry budget exhausted — `WorkBlock.RETRY_BUDGET_EXHAUSTED`
11. Missing selector metadata — `WorkBlock.SELECTOR_METADATA_MISSING`

Dynamic gates that depend on the whole graph snapshot (worker WIP caps,
completion backpressure, lease liveness) remain in `rotisserie.domain.policy`
(`allocate_capacity`, `apply_intake_pressure`, `lease_decision`) and the
application coordination layer. They are applied on top of the per-node verdict,
not inside it.

## Mapping to ComicPile

| ComicPile static exclusion (`factory_eligibility.py`) | Rotisserie signal | Exclusion code |
|---|---|---|
| `state != OPEN` | `state is not WorkState.OPEN` | `NOT_OPEN` |
| `non_executable_numbers` membership | `product_excluded` | `PRODUCT_EXCLUSION` |
| `epic`/`prd` labels or `is_acceptance_parent(body)` | `parent` | `PARENT_WORK` |
| `is_manual_only(body)` directive | `human_gate` | `HUMAN_BOUNDARY` |
| `BLOCKED_LABELS` present | `blocked` | `BLOCKED` |
| `TERMINAL_LABELS` present | `terminal` | `TERMINAL` |
| `owner_of(labels)` is an active factory lease | `active_lease` | `ACTIVE_LEASE` |
| `parse_declared_dependencies(body)` still open | `unresolved_dependencies` | `DEPENDENCY_INCOMPLETE` |
| `suppressing_issue_numbers` membership (open canonical PR) | `implementation_exists` | `IMPLEMENTATION_EXISTS` |
| `no_diff_attempts >= no_diff_limit` | `retry_budget_exhausted` | `RETRY_BUDGET_EXHAUSTED` |
| `require_ralph_labels` and `ralph-task` / `ralph-status:pending` missing | `missing_selector_metadata` | `SELECTOR_METADATA_MISSING` |

## What stays in the adopter translator

The following semantics are host- or product-specific encoding. They are
deliberately **not** in the Rotisserie domain and remain the responsibility of
the adopter's translator layer (ComicPile owns these today):

- the structured manual-only HTML directive and its code-span/fenced-block
  exemptions (`<!-- factory-execution:manual-only -->` detection);
- dependency-prose parsing: inline `Depends on #N` leading clusters, dependency
  headings and list items, markdown-wrapped references, and the exclusion of
  casual `#N` mentions;
- factory ownership label vocabulary (`factory:unowned` / `factory:local` /
  `factory:<n>`) and the regexes that recognize an active lease owner;
- epic/PRD label names and the acceptance-parent body heuristic (acceptance
  language plus a checkbox child graph);
- blocked and terminal label sets (`factory:blocked`, `ralph-status:blocked`,
  `wontfix`, `invalid`, `duplicate`, `ralph-status:done`);
- the product-exclusion registry (which concrete nodes are non-executable);
- no-diff retry-window accounting and the trust test that only workflow-posted
  (`github-actions` app) comments move machine markers or consume retry budget;
- selector-scoped metadata requirements such as the Ralph queue's `ralph-task`
  and `ralph-status:pending` labels.

The translator decodes each of these into an `EligibilitySignals` value; the
domain then evaluates every adopter identically. This keeps ComicPile's label
and marker strings out of the portable core while preserving the exact decision
semantics across the shadow comparison boundary.
