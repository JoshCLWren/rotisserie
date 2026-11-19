# Local factory independent review

Local factories own authorized delivery through CI repair, independent review,
gated merge and production acceptance. Start eligible actual local reviewers
instead of waiting for scheduled fleet pickup. Remote intake backpressure does
not apply to locals; existing identity, lease, review, exact-head CI, merge and
production authorization gates remain unchanged.

## Executable ACP lane

Use a clean private review checkout at the exact PR head without an implementation
writer. Fetch main and the PR branch before pinning revisions. Prepare a small
local context file containing issue objective/acceptance, actionable review
threads, verified contributors and exact-head CI evidence. Exclude secrets,
production data and raw logs. Missing evidence is unavailable, not a presumed pass.

```bash
review_head="$(git rev-parse HEAD)"
node "${CODEX_HOME:-$HOME/.codex}/skills/crouton-factory-codex/scripts/local-factory-acp-review.mjs" \
  --cwd "$PWD" --head "$review_head" --base origin/main \
  --pr 123 --issue 122 --reviewer 'local-acp-review-session' \
  --context /path/to/sanitized-review-context.txt \
  --output /path/to/new-private-review-evidence-directory
```

Replace the example numbers and paths. The evidence directory must be new. Retain
the real process handle when starting in background and inspect `status.json`
while continuing other authorized work. The launcher has a 20-minute bound;
preserve evidence and investigate live session state before retrying a timeout.

The command starts actual `opencode acp`, initializes the protocol, creates a
session in the checkout, selects and confirms a live eligible model, submits a read-only prompt
with the pinned diff and captures the actual response. Process-local OpenCode
permission overrides allow read/glob/grep and disable shell, edit, delegation and
other tools. Permission requests and unsupported client methods are rejected.
Shared configuration is unchanged. These controls are not an OS sandbox: use a
checkout without credential files and supply no secrets in context. If permitted
reads cannot establish a verdict, report blocked rather than expanding access.

Private evidence (`0700` directory/`0600` files): `initialize.json`, `session.json`,
`selection.json`, `prompt.txt`, `completion.json`, `status.json`, `pid`, `report.md`.
Raw protocol/provider logs and transcripts are not retained. Read **only
checkpoints/model-selection metadata and final reports/verdicts**, never full
ACP logs. Prompt/diff artifacts are local provenance, not coordinator/GitHub dumps.

Blocked verdicts, failed model selection, abnormal completion, missing verdicts,
changed checkouts and timeouts exit unsuccessfully. Only normal completion,
`state=complete`, confirmed model and the captured report establish actual review.
Synthetic protocol tests validate the launcher, never an independent approval.

### Diagnosing a blocked prompt

`status.json` records the structured ACP error cause in `reason`, for example
`ACP session/prompt failed (code -32603 provider.free-limit)`. An advertised free
model can be quota-limited at runtime; automatic selection cannot detect that in
advance. Supply an advertised model with available quota explicitly, e.g.
`--model opencode/hy4-flash-free` (or `--model opencode-go/hy4-flash-free`). Only
advertised models are accepted; an unavailable override fails without substitution.
The default selection is unchanged and the actual confirmed model is recorded in
`model-choice.json`.

## Manual local review ownership

Re-read labels immediately before claiming; preserve another live owner.
Atomically replace full issue/PR label sets, preserving unrelated labels, with
`factory`, `factory:review`, `factory:local` (issue `ralph-status:in-review`) only
while actual local review runs. Post a truthful start comment with coordinator
identity, independent reviewer/model, confirmed ACP session, exact head, timestamp
and evidence directory. Verify independence from every head contributor. Never
invent a numbered worker, trusted controller marker or native approval.

Crouton uses two independent eligible lanes: live-model ACP and Codex. Re-read remote
head before recording real results. Preserve reviewer identities and exact-head
evidence, resolve actionable findings, and retain controller/native review gates
where canonical policy requires them. A local report is not a fabricated trusted
controller verdict. Release ownership atomically to the truthful stage with
`factory:unowned` when review ends. A push invalidates previous CI and review.

## Install the Codex adapter

Tracked source: `.agents/skills/crouton-factory-codex/SKILL.md`. Install without
changing the original external Crouton skill:

```bash
install -d -m 755 "${CODEX_HOME:-$HOME/.codex}/skills/crouton-factory-codex/references" \
  "${CODEX_HOME:-$HOME/.codex}/skills/crouton-factory-codex/scripts"
install -m 644 .agents/skills/crouton-factory-codex/SKILL.md \
  "${CODEX_HOME:-$HOME/.codex}/skills/crouton-factory-codex/SKILL.md"
install -m 644 .agents/skills/crouton-factory-codex/references/review.md \
  "${CODEX_HOME:-$HOME/.codex}/skills/crouton-factory-codex/references/review.md"
install -m 644 .agents/skills/crouton-factory-codex/scripts/local-factory-acp-review.mjs \
  "${CODEX_HOME:-$HOME/.codex}/skills/crouton-factory-codex/scripts/local-factory-acp-review.mjs"
```

Repository `AGENTS.md` routes Comic Pile local factory requests to this adapter.
Installation grants no mutation authority. Official guidance explains
[skill discovery](https://learn.chatgpt.com/docs/build-skills) and
[AGENTS.md discovery](https://learn.chatgpt.com/docs/agent-configuration/agents-md).

## Live model selection

Omit `--model` for automatic selection from the live session's advertised model
options. Eligible defaults follow the factory's OpenCode free-name and OpenRouter
free-tier policy; unknown providers and paid models are excluded. The current
eligible model wins, otherwise OpenCode is preferred before OpenRouter with stable
ordering. No static model list or discontinued default is required.

An explicitly authorized `--model provider/id` overrides automatic selection only
when that exact model is advertised. Unavailable overrides fail without substitution.
No eligible models or failed confirmation stops the lane; never expand provider
permissions or spend to obtain a verdict. One session is attempted, without a model
retry loop. `model-choice.json`, `selection.json` and final `status.json` record the
actual selection. Runtime provider failure remains unavailable review.
