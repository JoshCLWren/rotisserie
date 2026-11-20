# Local factory runs

Run the real fixed-model worker on one adopter issue or PR on this machine, with no GitHub Actions run.

```bash
factory/local/run-worker.sh --repo OWNER/NAME --issue 1
factory/local/run-worker.sh --repo OWNER/NAME --pr 12 --model qwen3-coder:30b --budget 2400
factory/local/run-worker.sh --repo OWNER/NAME --pr 12 --stage factory:review --offline
```

## What is real

- The worker script, its primitives, the checkpoint writer, the scope guard, the fast checks, and the review controller. These come from this checkout, overlaid as the workflow does.
- OpenCode 2, installed by the production `install-opencode-v2.sh`, with its production config.
- The provider path: the `ollama-cloud` registry entry, with its URL pointed at the Ollama on this machine.
- The model: a local Ollama model, `qwen3-coder:30b` by default.

## What is local

- **GitHub.** The container has no token and no real `gh`. Its `gh` posts each call to `gh_proxy.py` on the host.
  - Reads run once with your host `gh` login and are saved under `fixtures/`. Later runs reuse the saved responses.
  - `--offline` never calls GitHub. A missing read fails loudly and is listed in `misses.txt`.
  - Writes never leave the machine. They are logged to `writes.jsonl` and answered with a synthetic success.
  - Label writes update a local overlay, so later reads see them. The target starts with the worker's owner label and stage.
  - `api.github.com` and `github.com` resolve to 127.0.0.1 inside the container.
- **git.** `origin` is a local bare repo at `origin.git`, seeded from `--adopter` (a local clone) or from GitHub, then `main` and the PR head. Checkpoints and pushes land there.
- **Neon** is not used.

## Output

Everything goes to `~/.cache/rotisserie-local/<repo>/<pr|issue>-<n>/`:

| File | Content |
|---|---|
| `worker.log` | The full worker log |
| `opencode-factory-*.log` | The model session |
| `writes.jsonl` | Every GitHub write the worker attempted |
| `worktree.diff` | The model's uncommitted edits at exit |
| `origin.git` | Local pushes and checkpoint refs |

## Requirements

- Docker
- Ollama on port 11434 with the model pulled
- `gh` logged in on the host, for the first read of each response
