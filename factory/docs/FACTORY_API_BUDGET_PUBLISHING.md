# Factory API budget publication

This is a trusted ComicPile workflow, not a Rotisserie background controller.
Rotisserie supplies tested, portable aggregation and sanitization code.

## Data flow

1. ComicPile's `factory-api-quota-observation.yml` queries `GET /rate_limit` using **that job's** `GITHUB_TOKEN` at minute 17 each hour and writes a private two-day Actions artifact named `factory-api-quota`. It does not measure operator `gh` credentials, PAT activity, or each worker App independently.
2. ComicPile's `publish-factory-api-budgets.yml` runs **when a successful `Factory API Quota Observation` workflow completes on `main`**, or by manual dispatch after explicitly enabled. It does not run on a separate clock, so the publisher sees the finalized observation artifact rather than racing the observer. It checks out a pinned Rotisserie commit with the existing private `ROTISSERIE_DEPLOY_KEY`.
3. The publisher uses a **source-only** short-lived App installation token to read `comic-pile` Actions artifacts, and a distinct **destination-only** token to update `JoshCLWren.github.io/factory-health/data.json`. <!-- docs-path-check: ignore -->
4. The script downloads at most 26 recent observations, calculates 24-hour minimum remaining REST/GraphQL/search budgets, counts observed transitions to REST exhaustion, and refuses to publish when data is missing, invalid, or over two hours old.
5. The static GitHub Pages frontend and the 5:30 AM daily report read only the sanitized public JSON. Neither polls the installation API.

## Provisioning (must be done by a GitHub account administrator)

Create a dedicated GitHub App that is installed on **both** `JoshCLWren/comic-pile` and `JoshCLWren/JoshCLWren.github.io`. Grant repository **Actions: Read** and **Contents: Read and write** at the App level. The workflow requests only Actions read on ComicPile and Contents write on the public site in **separate, repository-scoped tokens**. Do not reuse a factory worker App key or a personal access token.

In **ComicPile's Actions secrets** configure:
- `FACTORY_OBSERVABILITY_APP_ID`: the dedicated App's numeric ID.
- `FACTORY_OBSERVABILITY_APP_PRIVATE_KEY`: that App's private key. Never paste the key in an issue, a job log, or the Pages repository.

Confirm the existing `ROTISSERIE_DEPLOY_KEY` can read private Rotisserie. In **ComicPile's Actions variables**, set `FACTORY_OBSERVABILITY_ENABLED=true` only after the App is installed and the required secrets exist.

Before enabling, verify that a successful `Factory API Quota Observation` workflow has produced a private `factory-api-quota` artifact. After enabling, wait for the next successful observation (or trigger one manually) and inspect the resulting `Publish Factory API Budgets` run and confirm the public JSON has `schema_version: 1`, fresh `published_at`, accurate `installations[].observed_at`, and no secrets/private repository metadata.

## Failure and rollback

- Missing App credentials: publisher job does not run until enabled; if enabled while unconfigured it fails rather than falling back to a personal token.
- Missing/stale source observations or malformed archives: publication fails closed, leaving the old public JSON unchanged.
- HTTP 403/429: do not retry aggressively or substitute user credentials; examine installation quota reset and worker attribution separately.
- To stop publishing immediately, set `FACTORY_OBSERVABILITY_ENABLED=false` in ComicPile Actions variables. No secret needs to be placed in the public Pages repo.

**Limitations:** the API's `/rate_limit` response gives remaining budget, not endpoint-level top consumers. Only observed exhaustion transitions are counted. GitHub Actions billing usage and AI provider quotas remain unavailable until supported telemetry sources are integrated. Treat absent measurements as unavailable, not healthy.
