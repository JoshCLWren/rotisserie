#!/usr/bin/env bash
# Trusted pre-push fast checks for factory worker diffs.
#
# Worker wrappers source this file at startup from the trusted main checkout
# (before checkout_target switches onto a PR branch) and call
# factory_run_fast_checks right before committing an agent diff. A failure
# means the wrapper must not push: broken syntax, lint, types, or a failing
# targeted test never reaches a PR (examples that got through before: a
# duplicate `const` in generate-route-html.mjs on #3879 and a removed import
# that was still called on #3895).
#
# Checks, chosen by touched path (added/copied/modified/deleted, renames as
# delete + add, and untracked). A deleted .py or frontend file still selects
# the whole-project checks (ruff/ty, frontend typecheck) because deleting a
# still-imported module breaks its importers; file-level commands (node
# --check, eslint, targeted pytest) only receive files that exist.
#   *.mjs / *.cjs                 node --check <file>
#   *.py                          scripts/check-python-ci-lint.sh (exact CI
#                                 parity: ruff check . and ty check
#                                 --error-on-warning), then targeted pytest:
#                                 touched test_*.py files plus tests/test_<stem>.py
#                                 for each touched module
#   frontend/**/*.ts|tsx|mjs      pnpm --filter frontend run typecheck, then
#                                 eslint on the touched frontend files
#   *.rs / Cargo manifests        pinned Rust format, Clippy and root Vercel
#                                 compile checks (both actual CI crates)
#   Rust or vercel.json           native ingress, mutation-coverage and Vercel
#                                 deployment contract tests
# The worker runner (ubuntu-latest) has no uv/pnpm or dependencies, so the
# needed toolchain is installed on demand. Toolchain setup failure fails
# closed: an unverifiable diff is not pushed. A targeted pytest failure whose
# output is only a database connection error is reported but not blocking,
# because worker runners have no PostgreSQL service (CI still runs it).
# FACTORY_FAST_CHECKS=off disables the gate for an incident.

# Enable globstar for recursive glob patterns in case statements
shopt -s globstar

FACTORY_FAST_CHECK_TIMEOUT="${FACTORY_FAST_CHECK_TIMEOUT:-600}"
FACTORY_FAST_CHECK_DB_ERROR_RE='(ConnectionRefusedError|could not connect to server|Connect call failed|connection to server at .* failed|OSError: \[Errno 111\]|asyncpg\.exceptions\.(CannotConnectNowError|InvalidPasswordError|ConnectionDoesNotExistError))'

# Print touched paths relative to the repository root, one per line.
factory_fast_check_paths() {
  local base_ref="$1"
  {
    git diff --name-only --no-renames --diff-filter=ACMD "$base_ref" 2>/dev/null
    git ls-files --others --exclude-standard 2>/dev/null
  } | awk 'NF' | sort -u
}

# Classify touched paths (stdin) into "<kind>\t<path>" lines. Kinds: node,
# python, pytest, frontend, rust, ingress. A path may produce several kinds.
factory_fast_check_plan() {
  local path stem candidate
  while IFS= read -r path; do
    [[ -n "$path" ]] || continue
    case "$path" in
      *.mjs|*.cjs) printf 'node\t%s\n' "$path" ;;
    esac
    case "$path" in
      frontend/*.ts|frontend/*.tsx|frontend/*.mjs) printf 'frontend\t%s\n' "$path" ;;
    esac
    # A manifest-only change must not bypass pnpm's frozen-lockfile gate.
    case "$path" in
      package.json|pnpm-lock.yaml|pnpm-workspace.yaml|frontend/package.json)
        printf 'dependencies\t%s\n' "$path"
        ;;
    esac
    case "$path" in
      *.rs|Cargo.toml|*/Cargo.toml|Cargo.lock|*/Cargo.lock|rust-toolchain.toml|*/rust-toolchain.toml|.cargo/config.toml|*/.cargo/config.toml)
        printf 'rust\t%s\n' "$path"
        ;;
    esac
    case "$path" in
      vercel.json|api/*.rs|native/roll-service/src/*.rs)
        printf 'ingress\t%s\n' "$path"
        ;;
    esac
    # Check the generated OpenAPI contract (built from the checked-in
    # native/contracts/*.openapi.json, no Python) before a worker pushes.
    case "$path" in
      native/contracts/*.openapi.json|scripts/export_openapi_schema.mjs|scripts/generate_openapi_types.mjs|scripts/generate_native_client.mjs|frontend/src/generated/openapi.json|frontend/src/generated/openapi.ts|frontend/src/generated/nativeV2Client.ts)
        printf 'openapi\t%s\n' "$path"
        ;;
    esac
    case "$path" in
      *.py)
        printf 'python\t%s\n' "$path"
        stem="${path##*/}"
        stem="${stem%.py}"
        if [[ "$stem" == test_* || "$stem" == *_test ]]; then
          printf 'pytest\t%s\n' "$path"
        else
          candidate="tests/test_${stem}.py"
          [[ -f "$candidate" ]] && printf 'pytest\t%s\n' "$candidate"
        fi
        ;;
    esac
  done | sort -u
}

_factory_fast_check_budget() {
  local budget="$FACTORY_FAST_CHECK_TIMEOUT" left
  if declare -F remaining >/dev/null 2>&1; then
    left=$(( $(remaining) - 90 ))
    (( left < budget )) && budget="$left"
  fi
  (( budget < 30 )) && budget=30
  printf '%s\n' "$budget"
}

_factory_fast_check_run() {
  # Usage: _factory_fast_check_run LOG LABEL cmd...
  local log="$1" label="$2" status=0
  shift 2
  printf '\n### %s\n$ %s\n' "$label" "$*" >> "$log"
  timeout "$(_factory_fast_check_budget)" "$@" >> "$log" 2>&1 || status=$?
  printf '[exit %s]\n' "$status" >> "$log"
  return "$status"
}

_factory_fast_check_ensure_uv() {
  [[ ":${PATH}:" == *":${HOME}/.local/bin:"* ]] || export PATH="${PATH}:${HOME}/.local/bin"
  command -v uv >/dev/null 2>&1 && return 0
  python3 -m pip install --quiet --user --break-system-packages uv >/dev/null 2>&1 \
    || python3 -m pip install --quiet --user uv >/dev/null 2>&1 \
    || pipx install uv >/dev/null 2>&1 \
    || return 1
  command -v uv >/dev/null 2>&1
}

_factory_fast_check_ensure_pnpm() {
  command -v pnpm >/dev/null 2>&1 && return 0
  corepack enable >/dev/null 2>&1 \
    || { npm install -g corepack >/dev/null 2>&1 && corepack enable >/dev/null 2>&1; } \
    || true
  command -v pnpm >/dev/null 2>&1
}

# Ensure the Python toolchain the trusted pre-commit hook needs is installed.
# The hook runs scripts/lint.sh, which requires an activated .venv (or ruff in
# PATH) before it looks at which file types were staged, so even a docs- or
# frontend-only diff must have .venv available before it can be committed.
factory_ensure_commit_hook_toolchain() {
  [[ -x .venv/bin/ruff ]] && return 0
  _factory_fast_check_ensure_uv || return 1
  uv sync --frozen --all-extras --quiet
}

# Run the fast checks for the diff against BASE_REF. Writes a full log to
# $2 (default /tmp/factory-fast-checks-<worker>.log) and returns non-zero when
# any check (or the toolchain it needs) fails.
factory_run_fast_checks() {
  local base_ref="$1" log="${2:-/tmp/factory-fast-checks-${WORKER:-agent}.log}"
  local plan failures=() file status=0
  local -a node_files=() py_files=() pytest_files=() fe_files=()
  : > "$log"
  case "$(printf '%s' "${FACTORY_FAST_CHECKS:-on}" | tr '[:upper:]' '[:lower:]')" in
    0|off|false|no) printf 'fast checks disabled by FACTORY_FAST_CHECKS\n' >> "$log"; return 0 ;;
  esac
  plan="$(factory_fast_check_paths "$base_ref" | factory_fast_check_plan)"
  local fe_project=0 rust_project=0 rust_ingress=0 openapi_check=0 deps_project=0
  while IFS=$'\t' read -r kind file; do
    [[ -n "$file" ]] || continue
    case "$kind" in
      node) [[ -f "$file" ]] && node_files+=("$file") ;;
      python) py_files+=("$file") ;;
      pytest) [[ -f "$file" ]] && pytest_files+=("$file") ;;
      frontend)
        fe_project=1
        [[ -f "$file" ]] && fe_files+=("${file#frontend/}")
        ;;
      rust) rust_project=1 ;;
      ingress) rust_ingress=1 ;;
      openapi) openapi_check=1 ;;
      dependencies) deps_project=1 ;;
    esac
  done <<< "$plan"
  printf 'fast-check plan:\n%s\n' "${plan:-(no checked file types touched)}" >> "$log"

  for file in "${node_files[@]}"; do
    _factory_fast_check_run "$log" "node --check ${file}" node --check "$file" || failures+=("node --check ${file}")
  done

  if (( ${#py_files[@]} )); then
    if ! _factory_fast_check_ensure_uv; then
      failures+=('python toolchain setup (uv)')
    elif ! _factory_fast_check_run "$log" 'uv sync' uv sync --frozen --all-extras --quiet; then
      failures+=('python toolchain setup (uv sync)')
    else
      if [[ -f scripts/check-python-ci-lint.sh ]]; then
        VIRTUAL_ENV="$PWD/.venv" PATH="$PWD/.venv/bin:$PATH" \
          _factory_fast_check_run "$log" 'ruff + ty (CI parity)' bash scripts/check-python-ci-lint.sh \
          || failures+=('ruff/ty (scripts/check-python-ci-lint.sh)')
      else
        _factory_fast_check_run "$log" 'ruff check .' uv run --frozen ruff check . || failures+=('ruff check .')
        _factory_fast_check_run "$log" 'ty check' uv run --frozen ty check --error-on-warning || failures+=('ty check --error-on-warning')
      fi
      if (( ${#pytest_files[@]} )); then
        status=0
        _factory_fast_check_run "$log" 'targeted pytest' \
          uv run --frozen pytest -q -p no:cacheprovider --no-cov -x "${pytest_files[@]}" || status=$?
        if (( status != 0 )); then
          if (( status != 124 )) && grep -Eq "$FACTORY_FAST_CHECK_DB_ERROR_RE" "$log" \
              && ! grep -Eq '^(E +)?(AssertionError|NameError|ImportError|ModuleNotFoundError|SyntaxError|TypeError|AttributeError)' "$log"; then
            printf 'targeted pytest failed only on database connectivity; not blocking (no PostgreSQL on the worker runner)\n' >> "$log"
          else
            failures+=("targeted pytest (${pytest_files[*]})")
          fi
        fi
      fi
    fi
  fi

  # App changes can break the frontend API contract without changing a single
  # frontend file. Check generated schema freshness before GitHub CI sees it.
  if (( openapi_check )); then
    if ! command -v node >/dev/null 2>&1; then
      failures+=('OpenAPI check toolchain (node)')
    elif ! _factory_fast_check_run "$log" 'OpenAPI schema freshness' \
      node scripts/export_openapi_schema.mjs --check; then
      failures+=('OpenAPI schema freshness')
    fi
  fi

  if (( fe_project || deps_project )); then
    if ! _factory_fast_check_ensure_pnpm; then
      failures+=('frontend toolchain setup (pnpm)')
    elif ! _factory_fast_check_run "$log" 'pnpm install' pnpm install --frozen-lockfile --silent; then
      failures+=('frontend toolchain setup (pnpm install)')
    else
      _factory_fast_check_run "$log" 'frontend typecheck' pnpm --filter frontend run typecheck \
        || failures+=('frontend typecheck')
      if (( ${#fe_files[@]} )); then
        _factory_fast_check_run "$log" 'frontend eslint (touched files)' pnpm --dir frontend exec eslint -- "${fe_files[@]}" \
          || failures+=("frontend eslint (${fe_files[*]})")
      fi
    fi
  fi


  # Rust-only changes previously bypassed this trusted gate entirely.
  # Check both native packages through the shared root Cargo workspace.
  # The script fails closed on missing toolchains and failed compilation.
  if (( rust_project )); then
    if ! command -v cargo >/dev/null 2>&1 || ! command -v rustup >/dev/null 2>&1; then
      failures+=('Rust toolchain (cargo/rustup missing)')
    elif ! _factory_fast_check_run "$log" 'Rust workspace format, Clippy, and compilation' \
      bash scripts/check-rust-factory-prepush.sh; then
      failures+=('Rust format/Clippy/native build')
    fi
  fi

  # When native routing or Rust handlers change, verify the real Vercel
  # rewrite, HTTP-method registration and POST/PATCH write-path guards.
  if (( rust_project || rust_ingress )); then
    if ! _factory_fast_check_ensure_uv; then
      failures+=('Rust ingress test toolchain (uv)')
    elif ! _factory_fast_check_run "$log" 'Rust ingress test dependency sync' \
      uv sync --frozen --all-extras --quiet; then
      failures+=('Rust ingress test dependencies')
    elif ! _factory_fast_check_run "$log" 'Native ingress and deployment contract tests' \
      uv run --frozen pytest -q -x --no-cov \
        tests/test_native_roll_live_ingress.py \
        tests/test_native_roll_write_test_guard.py \
        tests/test_vercel_deployment.py; then
      failures+=('native ingress/deployment/write guard tests')
    fi
  fi

  if (( ${#failures[@]} )); then
    printf '\nFAST CHECKS FAILED: %s\n' "$(IFS=';'; echo "${failures[*]}")" >> "$log"
    printf 'factory fast checks failed: %s (log: %s)\n' "$(IFS=';'; echo "${failures[*]}")" "$log" >&2
    return 1
  fi
  printf '\nFAST CHECKS PASSED\n' >> "$log"
  return 0
}

# Neutralize factory markers and HTML comments in untrusted text before a
# trusted (github-actions) comment repeats it.
factory_neutralize_markers() {
  printf '%s' "$1" | sed -e 's/<!--/\&lt;!--/g' -e 's/-->/--\&gt;/g' \
    -e 's/comic-pile-factory-/comic-pile-factory‑/g' -e 's/```/` ` `/g'
}

# Tell the next worker why this diff was not pushed. Best effort.
#   $1 issue|pr   $2 number   $3 log file
factory_report_fast_check_failure() {
  local kind="$1" number="$2" log="$3" body tail_text
  tail_text="$(tail -n 80 "$log" 2>/dev/null | cut -c1-400)"
  tail_text="$(factory_neutralize_markers "$tail_text")"
  body="$(printf '<!-- comic-pile-factory-fast-checks-failed-v1 -->\n### Factory pre-push fast checks failed\n\nFactory `%s` did not push its %s change for #%s because the trusted pre-push fast checks failed (see docs/AUTONOMOUS_FACTORY_POLICY.md, "Pre-push fast checks"). The working tree was reset; the next attempt must fix these failures.\n\n<details><summary>Fast-check log tail</summary>\n\n```text\n%s\n```\n</details>\n' \
    "${WORKER:-agent}" "$kind" "$number" "$tail_text")"
  gh issue comment "$number" --body "$body" >/dev/null 2>&1 || true
}
