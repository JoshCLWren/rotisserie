# Rust API cutover: endpoint implementation pattern

Use this as an implementation checklist, not an alternative API architecture. The
authoritative product contract is #4335 and its latest owner decisions. Preserve
the precise resource-oriented v2 contract and migrate the **actual frontend
caller**. Do not create a parallel unused endpoint, temporary feature flag, or
Python fallback.

## Copy the existing working structure

1. Start from the current canonical Rust service:
   `native/roll-service/src/lib.rs`. For owned issue writes, inspect
   `issue_handler.rs` and its transaction-bound `issue_service.rs::persist`.
   For batch reads, inspect `reading_boundary.rs`. Preserve the established
   `AuthenticatedUser` extractor, `AppState`, `ApiError`, bounded DB pool
   and owner-scoped SQL. Never hardcode a user ID or return mock success values.
2. Define the request and response using the canonical OpenAPI shape. Validate
   bounds, errors and 404/422 behavior against the existing route and frontend
   expectations, including null versus absent JSON fields.
3. Add the exact HTTP **method and path** in the service router in `lib.rs`,
   then register the same handler in the real ingress (`api/native_roll.rs`
   or `api/native_issue.rs`) and add the method-specific `vercel.json`
   rewrite **before** the catch-all. Test both allowed and rejected methods,
   numeric IDs, route precedence and security headers.
4. Trace and switch the real caller under `frontend/src/services/`. Update
   generated API types, tests, and API oracle/parity tests as needed. Do not
   infer an implementation language from an API version.
5. For writes, use owner-scoped SQL within a transaction, serialize concurrent
   modifications when necessary, persist idempotency receipts, and check the
   resulting database state. A mock, auth-only or read-only test does not prove
   a mutation. The HTTP write-path guard covers POST, PATCH, PUT and DELETE.
6. Protect the exact behavior with disposable-PostgreSQL tests and targeted
   frontend/ingress checks. For direct read/rate, see
   `native/roll-service/src/issue_service.rs` and
   `native/roll-service/parity/test_http_issue_read_rate.py`. For ingress
   checks, see `tests/test_native_roll_live_ingress.py`,
   `tests/test_native_roll_write_test_guard.py`, and
   `tests/test_vercel_deployment.py`.
7. Deploy only after exact-head CI and independent semantic review. Confirm a
   successful authenticated production request used the Rust entrypoint and
   achieved the requested behavior. Keep the owner objective open until
   applicable caller migration, deletion and production acceptance are done.

## Conflict-resistant ingress tests (required for every new Rust endpoint)

Native cutovers change a shared `vercel.json` route list, but they must **not**
renumber unrelated test expectations. Use the method/path/entrypoint discovery
in `tests/test_native_roll_live_ingress.py` for the complete Rust surface;
use the semantic route lookup in `tests/test_vercel_deployment.py` for
specific contract and security-header checks. **Do not introduce
`routes[37]`-style assertions, fixed native-route counts, or mass index
updates** when adding an endpoint. Any test added for an endpoint should
probe its real HTTP method, numeric ID validation and wrong-method fallback.

The deploy regression resolves exactly one route by `src` (plus HTTP method
when needed) and verifies that all native rewrites precede the Python
catch-all, which itself precedes the static/SPA routes. Missing or duplicate
routes, shadowing, and missing security headers must continue to fail; an
unrelated route inserted before the catch-all must not.

Start new endpoint branches at current `main`. Before requesting exact-head
review, reconcile branch drift or genuine conflicts with the latest `main`,
then rerun local routing tests and CI on the final head. **Do not enable
automatic rebases for every open PR on each main update**: that repeatedly
invalidates exact-head CI, independent reviews and concurrent factory leases.
Use the normal mergeability/ready-drain gate and resolve conflicts when
needed. Preserve all newer routes when reconciling an older PR; never restore
an obsolete `vercel.json` wholesale.

## Pre-push checks

From the repository root:

```bash
bash scripts/check-rust-factory-prepush.sh
uv run --frozen pytest -q -x --no-cov \
  tests/test_native_roll_live_ingress.py \
  tests/test_native_roll_write_test_guard.py \
  tests/test_vercel_deployment.py
```

The first command checks the whole Cargo workspace through the shared root
toolchain and lockfile, matching `.github/workflows/rust-roll.yml` and
`.github/workflows/native-vercel-rust.yml` (root `rust-toolchain.toml`:
floating stable).
The trusted factory wrapper runs these checks automatically for Rust or native
ingress changes. Targeted service tests requiring `RUST_ROLL_TEST_DATABASE_URL`
must use **disposable** PostgreSQL, never the production database. Full suite,
parity, migration health, and the remaining gates run in CI.

The worker's pre-push toolchain is a prerequisite, not evidence that CI passed.
If compilation cannot run, fail closed and preserve a truthful handoff rather
than pretending the code was verified or weakening the tests.

## Quality measurement

The read-only first-head scorecard runs daily from
`.github/workflows/rust-first-head-quality.yml`. Its rolling seven-day JSON
and Markdown artifacts are retained for 30 days and identify the declared
model and worker where supplied by the PR body.

To inspect the latest retained GitHub Actions history manually:

```bash
python scripts/report_rust_first_head_ci.py --since 2026-10-29 --format markdown
```

The report distinguishes passed, failed, cancelled, incomplete, and missing
first-head runs. Failed job names are shown, but **code versus infrastructure**
causality remains unclassified unless someone investigates the actual logs.
Its bounded GitHub query window and retained-run limitations are explicit.
It does not change merge gates, schedule factory dispatch, or write to GitHub.
