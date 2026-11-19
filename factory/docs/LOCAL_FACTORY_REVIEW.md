# Local factory independent review

The maintained instructions and launcher ship with the
[Codex Crouton adapter](../.agents/skills/crouton-factory-codex/SKILL.md).
See its [executable review path and installation instructions](../.agents/skills/crouton-factory-codex/references/review.md).

The repository command `node scripts/local-factory-acp-review.mjs` invokes that
single maintained implementation. Installed skills also include the same launcher
and reference, so local review does not depend on an unmerged repository wrapper.

## Import actual local reviews into canonical gates

After the two actual local lanes complete, the repository owner can dispatch
`factory-local-review.yml` on `main` with the `evidence` JSON input. The workflow
uses trusted main code and its own bot credential; PR code never executes in
this attestation job. The importer verifies the GitHub run's actor, event,
workflow path and branch. Owner submission attests that the supplied original
reports and session metadata are genuine; this is not a new model review.

The payload contains `pr`, `issue`, full `head`, every local author/repairer in
`contributors`, `acceptance_reviewer` naming the actual lane that produced the
acceptance, the original independent reviewer's structured `acceptance`
object, and exactly two `lanes`. Every lane has a truthful `local-*` `identity`,
`head`, `kind` (`alpha-acp` or `codex`), actual `model`, `verdict: "approve"`, and
original `report`, and `state: "complete"`. The ACP lane additionally has its actual `session`, and `model_confirmed: true` from the launcher metadata; its
original report must end in `FACTORY_GATE_READY`. Never relabel a coordinator's
acceptance report as Alpha output. Do not submit secrets, raw transcripts, logs,
or production data. Incomplete or stale reports require fresh actual evidence.

Release completed local review leases before submission. The importer rejects all active-owner conflicts and contributor self-review,
validates the original acceptance object against the fresh issue revision and
head, and records truthful local reviewer identities using existing semantic,
model and acceptance attestation formats. It then releases the PR to canonical
`factory:ci` reconciliation and wakes the existing merge drain. CI, implementation
delivery, unresolved feedback, dependency, main-health and production gates
remain authoritative. This workflow does not merge directly and cannot waive
post-deployment acceptance requirements.

The historical `alpha-acp` lane kind is a wire identifier, not a model requirement.
Use live-model selection from the maintained launcher; record the actual confirmed
model rather than inferring it from the lane kind.
