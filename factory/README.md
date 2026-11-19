# Rotisserie factory runtime

This directory is the factory runtime that adopter repositories install. It
came from JoshCLWren/comic-pile at `7ba8423` (see `docs/migration/`), keeps that
repository's layout so relative paths still work, and is configured per adopter.

The workflow files under `.github/workflows/` here are templates. GitHub does
not run them in Rotisserie. They run in each adopter repository, on that
repository's events and Actions minutes.

## How an adopter runs it

1. The adopter commits `.rotisserie/config.toml` and the installed workflows.
2. Each installed workflow checks out the adopter, then checks out Rotisserie at
   a pinned commit into `.rotisserie/runtime/`, then runs
   `factory/install/overlay.sh`.
3. `overlay.sh` copies the paths in `install/overlay-paths.txt` into the
   workspace and lists them in `.git/info/exclude`, so workers never commit
   them. It refuses to run while the adopter still tracks any of those paths.

Adopter state stays tracked in the adopter: `.github/factory-*.json`,
`.github/NON_FACTORY_ACTIONS_PAUSED`, `FACTORY_GATE_READY`, and the adopter's
own `docs/ISSUE_EXECUTION_PROTOCOL.md` and `docs/PRODUCT_ACCEPTANCE_PROTOCOL.md`.

## Install or update

```bash
python3 factory/install/install.py /path/to/adopter --ref <rotisserie-commit-sha>
python3 factory/install/install.py /path/to/adopter --ref <sha> --workflow factory-ready-merge-drain.yml
```

`--ref` must be a full commit SHA. Installed files keep the template names, so a
workflow disabled in the adopter stays disabled when its file is updated.

## Configuration

`.rotisserie/config.toml` in the adopter:

```toml
schema_version = 1

[repository]
owner = "your-org"
name = "your-repo"

[production]
url = "https://your-app.example"

[markers]
namespace = "rotisserie-factory"

# Bring your own models. Entries merge over defaults/providers.toml.
[providers.my-gateway]
provider = "my-gateway"
credential = "MY_GATEWAY_API_KEY"
endpoint = "https://gateway.example/v1"
name = "My OpenAI-compatible gateway"

[providers]
disabled = ["pollinations"]
# mode = "replace"   # use only the providers declared here
```

Endpoints and `catalog_url` can name extra credentials as placeholders, for
example `{CLOUDFLARE_ACCOUNT_ID}`. In Actions, `GITHUB_REPOSITORY` wins over
`[repository]`; local runs read the file.

## Tests

```bash
cd factory
uv sync --locked
uv run --locked pytest -n auto
node --test .github/scripts/*.test.cjs scripts/local-factory-acp-review.test.mjs
```

The suite runs as the reference adopter in `tests/fixtures/adopter/config.toml`.
