# Worker 48 App provisioning from a terminal

Issue #3132 provisions only worker 48, using its persisted Mark Cordova identity.
This is an interactive tool run by Josh or an agent acting on his explicit session
instruction. Scheduled factory workers must not create or install Apps. Fleet
provisioning remains gated by #3137 and #3139.

## Start and resume

Requires Node.js 22 or newer, `gh` authenticated as JoshCLWren with repository
secret write access, and a browser signed into that account on the same machine.

```sh
node .github/scripts/provision-worker-app.cjs start
```

Open the printed loopback URL. The registration form supplies the persisted name,
autonomous-worker description, private visibility, no webhook configuration, and
repository permissions. GitHub generates the private key during manifest conversion, so no
manual key download or copying is necessary. After registration, follow the direct
installation link and select **only `comic-pile`**.

Contents and pull requests need write access for pushes, PR creation, and reviews;
issues need write access for the readable issue/PR handoffs. Metadata is read-only.
Worker Apps remain outside all trusted-marker allowlists. This tool does not change
controller authority, schedules, or the shared `PR_REBASE_TOKEN`.

Then run, or have the interactive agent run:

```sh
node .github/scripts/provision-worker-app.cjs finish
node .github/scripts/provision-worker-app.cjs status
```

`finish` verifies the App installation and that its installation token can access
exactly the intended repository. It uploads the three worker-48 secrets through
`gh secret set` using stdin, then persists only the App ID, bot login, and
installation ID in `.github/factory-worker-github-apps.json`. Commit that public
mapping after live verification. No private key or token belongs in the repository.

Restart `start` to resume an already registered App at installation. Repeat `finish`
after a failed secret upload; it does not register another App. Existing mapping
IDs must agree with the saved credentials. The helper refuses to register a mapped
App when its local credentials are missing.

## Local credentials and callbacks

Private state lives in `~/.local/state/comic-pile/worker-app-48`, with directory mode
0700 and credential files mode 0600. Preserve it through an interrupted setup; the
manifest conversion code is single-use. It is not printed by the helper or served
to the browser. Back up the private key in your normal secret store after success.

The server binds only to `127.0.0.1`. It checks the Host header and a random callback
state, disables caching and referrer propagation, and does not mutate secrets from
an installation callback. Finish runs explicitly in the owner terminal. Ctrl+C
stops the server without deleting saved progress.

Override the port or local state directory when needed:

```sh
node .github/scripts/provision-worker-app.cjs start --port 8766 --state-dir /private/path/worker-48
node .github/scripts/provision-worker-app.cjs finish --state-dir /private/path/worker-48
```

This helper does not automate browser clicks. In a session with a supported,
connected browser, an explicitly authorized interactive agent can drive those
steps. In a Linux terminal without that connection, the owner performs only the
registration and repository-installation confirmations. If the browser is on
another machine, its loopback URL will not reach this server; use a local checkout
there or forward the loopback port over SSH.

If GitHub reports a name collision, stop registration and use the existing
`bootstrap-collision 48` command with the pinned Faker 40.40.0 environment. Persist
that single fallback name before restarting; never invent another name or re-roll
an App that already exists. Inspect your existing Apps before retrying if manifest
conversion fails after creation; do not create a duplicate.

## Verification

```sh
node --test .github/scripts/provision-worker-app.test.cjs
```

Coverage exercises nonce validation, duplicate callback handling, owner/name/key
validation, repository scoping, private file permissions, secret upload through
stdin, partial upload recovery, and isolation from other worker mappings. Live
creation and installation are separate acceptance evidence; passing mocked tests
does not mean the GitHub App has been provisioned.

GitHub references: [manifest registration](https://docs.github.com/en/apps/sharing-github-apps/registering-a-github-app-from-a-manifest),
[repository secrets via CLI](https://cli.github.com/manual/gh_secret_set).
