# Rotisserie factory runtime

This directory is the factory runtime that adopter repositories install. It
is a copy of JoshCLWren/comic-pile's factory and keeps that repository's layout
so relative paths still work.

The factory workflows are reusable workflows in Rotisserie's root
`.github/workflows/`. Each has only an `on: workflow_call` trigger, so GitHub never
runs them in Rotisserie. They run in each adopter repository, on that
repository's events and Actions minutes.

## How an adopter runs it

1. The adopter commits `.rotisserie/config.toml` and the thin caller workflows
   from `install/callers/`. A caller holds only the triggers, permissions,
   concurrency, run-name, and one job that calls the reusable workflow of the
   same name with its inputs and the secrets it needs.
2. Each reusable job checks out the adopter, then checks out Rotisserie at the
   commit of the called workflow (`job.workflow_repository` at
   `job.workflow_sha`) into `.rotisserie/runtime/`, then runs
   `factory/install/overlay.sh`. Jobs that run Python set up Python 3.14 first.
3. `overlay.sh` copies the paths in `install/overlay-paths.txt` into the
   workspace and lists them in `.git/info/exclude`, so workers never commit
   them. It refuses to run while the adopter still tracks any of those paths.

Adopter state stays tracked in the adopter: `.github/factory-*.json`,
`.github/NON_FACTORY_ACTIONS_PAUSED`, `FACTORY_GATE_READY`, and the adopter's
own `docs/ISSUE_EXECUTION_PROTOCOL.md` and `docs/PRODUCT_ACCEPTANCE_PROTOCOL.md`.

## Install or update

```bash
python3 factory/install/install.py /path/to/adopter
python3 factory/install/install.py /path/to/adopter --ref <rotisserie-commit-sha>
python3 factory/install/install.py /path/to/adopter --workflow factory-ready-merge-drain.yml
```

`--ref` is `main` by default, or a branch name or full commit SHA. Callers keep
the reusable workflow names, so a workflow disabled in the adopter stays
disabled when its file is updated, and scripts that dispatch a workflow by file
name keep working. A full install also deletes workflow files that earlier
versions installed and that the reusable workflows replace.

Secrets pass explicitly, by name, from each caller. GitHub documents
`secrets: inherit` only for callers in the same organization or enterprise, and
explicit secrets give each reusable workflow only what it reads. Repository
variables need no passing: a called workflow reads the caller repository's
`vars`.

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

The runtime files are a verbatim copy of Comic Pile's factory at
`29fd7ce`, the commit before the cutover. They still name
`JoshCLWren/comic-pile`. Adopter configuration is tracked in issue #78.

## Tests

```bash
cd factory
uv sync --locked
uv run --locked pytest -n auto
node --test .github/scripts/*.test.cjs scripts/local-factory-acp-review.test.mjs
```

The suite runs as the fake adopter `acme/widgets` from `tests/fixtures/adopter/config.toml`.
