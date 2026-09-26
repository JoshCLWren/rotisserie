# Legacy snapshot migration inventory

This inventory classifies every artifact below `reference/legacy-factory/`. It
is rule-based so additions cannot silently go unclassified:
`tests/test_migration_inventory.py` requires every path to match an ordered
rule.

The classification describes the intended destination, not current runtime
status. Every artifact remains quarantined migration evidence until a later
issue explicitly replaces or removes it.

| Classification | Meaning |
|---|---|
| `migrate` | Behavior is a candidate for extraction into a generic domain, application, or adapter boundary. |
| `reference` | Keep as behavioral evidence or a regression oracle; do not expose it as product code. |
| `product-specific` | Legacy repository, host, provider, prompt, or workflow policy that does not belong in the generic core. |
| `retire` | Redundant migration-era material with no intended Rotisserie API. Preserve it until an issue authorizes removal. |

Rules are evaluated in the order shown below. The first match owns the path.

| Glob relative to `reference/legacy-factory/` | Classification | Intended treatment |
|---|---|---|
| `.github/scripts/factory_work_policy_latticery_ref.py` | `retire` | Superseded extraction reference; retain only for provenance. |
| `.github/scripts/factory_*policy.py` | `migrate` | Extract generic deterministic policy in phases 2–3. |
| `.github/scripts/factory_*controller.py` | `migrate` | Extract application use cases in phase 5. |
| `.github/scripts/factory-revocation-fence.py` | `migrate` | Preserve the mutation-fence behavior behind an application boundary. |
| `.github/scripts/stale_pr_decay.py` | `migrate` | Re-express generic recovery policy after graph contracts exist. |
| `.github/scripts/test_factory_*policy.py` | `reference` | Policy regression evidence for extraction. |
| `.github/scripts/test_factory_*controller.py` | `reference` | Controller regression evidence for extraction. |
| `.github/scripts/test_factory_review_thread_gate.py` | `reference` | Review-gate regression evidence. |
| `.github/scripts/test_factory_stage5.py` | `reference` | Integration regression evidence. |
| `tests/test_factory_*.py` | `reference` | Archived behavioral regression suite. |
| `tests/test_generate_factory_status_dashboard.py` | `reference` | Archived visibility regression evidence. |
| `tests/fixtures/**` | `reference` | Archived test inputs; sanitize before reuse. |
| `.github/workflows/**` | `product-specific` | Disabled host orchestration; never activate as Rotisserie workflows. |
| `.github/scripts/**` | `product-specific` | GitHub/provider/legacy runtime implementation not selected above. |
| `.github/**` | `product-specific` | Legacy configuration and operational state. |
| `.agents/**` | `product-specific` | Legacy worker skills and repository conventions. |
| `.claude/**` | `product-specific` | Provider-specific worker skills and links. |
| `.opencode/**` | `product-specific` | Provider-specific agent definitions. |
| `prompts/**` | `product-specific` | Legacy prompt policy; evidence only, never authority. |
| `scripts/tests/**` | `reference` | Archived runtime regression evidence. |
| `scripts/**` | `product-specific` | Legacy provider and repository runtime tooling. |
| `docs/**` | `reference` | Historical operating policy to classify during later extraction. |

The inventory deliberately has no catch-all rule. A new copied path must be
reviewed and assigned a treatment before the inventory test will pass.
