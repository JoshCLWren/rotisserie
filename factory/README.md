# Rotisserie factory runtime

This directory is the factory runtime that adopter repositories install. It
started as a copy of the first adopter's factory and keeps that layout so
relative paths still work.

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

## Adopter configuration

`.rotisserie/config.toml` in the adopter repository holds every
adopter-specific value. Only `[repository]` is required.

```toml
schema_version = 1

[repository]          # used when GITHUB_REPOSITORY is unset (local runs)
owner = "acme"
name = "widgets"

[issues]
reserved = []         # issues the factory never claims and never closes

[checks]
fast = ""             # replaces the built-in pre-push fast checks

[dashboard]
auth_url = ""         # https sign-in service for the hosted dashboard;
                      # empty keeps the dashboard read-only
```

## Comment markers

The factory records state in HTML comment markers on issues and pull
requests, such as `<!-- widgets-factory-merge-closure-v1:pr-7:issue-3 -->`.
Every marker starts with a prefix that `factory_adopter.marker_prefix()`
derives from the repository name (`acme/widgets` gives `widgets`). Because the
prefix comes from the name, an adopter keeps recognizing the markers it wrote
before. `FACTORY_MARKER_PREFIX` overrides it, for example after a rename.
`factory-visibility.cjs` and the shell workers follow the same rule.

## Scope

The runtime started as a copy of the first adopter's factory at `29fd7ce`, the
commit before the cutover. It names no adopter: the repository, comment-marker
prefix and reserved issues come from `GITHUB_REPOSITORY` and
`.rotisserie/config.toml`. Some built-in defaults still reflect the first
adopter's stack (for example the pre-push fast checks); `[checks] fast`
replaces them.

## Tests

```bash
cd factory
uv sync --locked
uv run --locked pytest -n auto
node --test .github/scripts/*.test.cjs scripts/local-factory-acp-review.test.mjs
```

The suite runs as the fake adopter `acme/widgets` from `tests/fixtures/adopter/config.toml`.
