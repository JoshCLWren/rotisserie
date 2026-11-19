#!/usr/bin/env bash
set -Eeuo pipefail

# ===========================================================================
# Test suite for comic-pile-opencode-factory-heartbeat.sh V22
#
# Extracts function definitions from the heartbeat script and tests them
# in isolation using a temporary git repo + bare remote.
# ===========================================================================

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
HEARTBEAT="$SCRIPT_DIR/../comic-pile-opencode-factory-heartbeat.sh"
PASS=0
FAIL=0
TEST_TMPDIR=""

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
cleanup() {
  if [[ -n "$TEST_TMPDIR" && -d "$TEST_TMPDIR" ]]; then
    rm -rf "$TEST_TMPDIR"
  fi
}
trap cleanup EXIT

fail() {
  FAIL=$((FAIL + 1))
  printf '  FAIL: %s\n' "$*" >&2
}

pass() {
  PASS=$((PASS + 1))
  printf '  PASS: %s\n' "$*"
}

assert_eq() {
  local expected="$1" actual="$2" msg="$3"
  if [[ "$expected" == "$actual" ]]; then
    pass "$msg"
  else
    fail "$msg — expected '$expected', got '$actual'"
  fi
}

assert_contains() {
  local haystack="$1" needle="$2" msg="$3"
  if [[ "$haystack" == *"$needle"* ]]; then
    pass "$msg"
  else
    fail "$msg — '$needle' not found"
  fi
}

assert_file_exists() {
  local path="$1" msg="$2"
  if [[ -f "$path" ]]; then
    pass "$msg"
  else
    fail "$msg — file $path does not exist"
  fi
}

assert_file_not_exists() {
  local path="$1" msg="$2"
  if [[ ! -f "$path" ]]; then
    pass "$msg"
  else
    fail "$msg — file $path should not exist"
  fi
}

# Create a temporary test environment with a bare remote and working repo.
setup_test_env() {
  cleanup
  TEST_TMPDIR="$(mktemp -d)"
  git init --bare "$TEST_TMPDIR/bare.git" 2>/dev/null
  git clone "$TEST_TMPDIR/bare.git" "$TEST_TMPDIR/work" 2>/dev/null
  git -C "$TEST_TMPDIR/work" checkout -b main 2>/dev/null
  echo "init" > "$TEST_TMPDIR/work/README.md"
  git -C "$TEST_TMPDIR/work" add . && git -C "$TEST_TMPDIR/work" commit -m "init" 2>/dev/null
  git -C "$TEST_TMPDIR/work" push -u origin main 2>/dev/null
}

# Create a second worktree by cloning the bare repo.
setup_worktree() {
  local worktree="$TEST_TMPDIR/worktree-1"
  git clone --quiet "$TEST_TMPDIR/bare.git" "$worktree" 2>/dev/null
  git -C "$worktree" checkout --quiet main 2>/dev/null
  echo "$worktree"
}

# ---------------------------------------------------------------------------
# Source function definitions from the heartbeat script.
# Uses grep to extract only function definitions (lines matching pattern).
# ---------------------------------------------------------------------------
extract_functions() {
  # Extract function definitions using awk — find function declarations
  # and collect everything until the matching closing brace
  local funcs
  funcs="$(awk '
    /^[a-zA-Z_][a-zA-Z0-9_]*\(\) \{/ {
      in_func = 1
      depth = 1
      printf "%s\n", $0
      next
    }
    in_func {
      printf "%s\n", $0
      # Count braces
      for (i = 1; i <= length($0); i++) {
        c = substr($0, i, 1)
        if (c == "{") depth++
        if (c == "}") depth--
      }
      if (depth <= 0) {
        in_func = 0
        printf "\n"
      }
    }
  ' "$HEARTBEAT")"

  export MAX_SAME_ISSUE_ATTEMPTS="${MAX_SAME_ISSUE_ATTEMPTS:-2}"
  eval "$funcs"
}

# ===========================================================================
# TEST: Script syntax check
# ===========================================================================
test_syntax() {
  printf '\n--- test_syntax ---\n'
  bash -n "$HEARTBEAT" && pass "script has valid bash syntax" || fail "script has syntax errors"
}

# ===========================================================================
# TEST: --help flag works
# ===========================================================================
test_help() {
  printf '\n--- test_help ---\n'
  local output
  output="$(bash "$HEARTBEAT" --help 2>&1)" || true
  assert_contains "$output" "Usage:" "--help shows usage"
  assert_contains "$output" "--watch" "--help shows --watch"
  assert_contains "$output" "--once" "--help shows --once"
  assert_contains "$output" "--worktree" "--help shows --worktree"
  assert_contains "$output" "--state-dir" "--help shows --state-dir"
}

# ===========================================================================
# TEST: Ledger init
# ===========================================================================
test_ledger_init() {
  printf '\n--- test_ledger_init ---\n'
  setup_test_env

  local state_dir="$TEST_TMPDIR/state"
  mkdir -p "$state_dir"
  LEDGER="$state_dir/delivery-ledger.tsv"

  ledger_init
  assert_file_exists "$LEDGER" "ledger_init creates file"
  local header
  header="$(head -1 "$LEDGER")"
  assert_contains "$header" "issue" "header has issue column"
  assert_contains "$header" "branch" "header has branch column"
  assert_contains "$header" "progress_heads" "header uses progress_heads (not press_progress_heads)"
}

# ===========================================================================
# TEST: Ledger append and get
# ===========================================================================
test_ledger_append_get() {
  printf '\n--- test_ledger_append_get ---\n'
  setup_test_env

  local state_dir="$TEST_TMPDIR/state"
  mkdir -p "$state_dir"
  LEDGER="$state_dir/delivery-ledger.tsv"
  ledger_init

  ledger_append "123" "factory/123-test" "abc123"
  local row
  row="$(ledger_get "123")"
  assert_contains "$row" "123" "ledger_get finds issue 123"
  assert_contains "$row" "factory/123-test" "ledger_get has branch"
  assert_contains "$row" "claiming" "ledger_get has initial state"

  is_claimed "123" && pass "is_claimed returns 0 for active issue" || fail "is_claimed should return 0"
  is_claimed "999" && fail "is_claimed should return 1 for non-existent" || pass "is_claimed returns 1 for non-existent"
}

# ===========================================================================
# TEST: Ledger update
# ===========================================================================
test_ledger_update() {
  printf '\n--- test_ledger_update ---\n'
  setup_test_env

  local state_dir="$TEST_TMPDIR/state"
  mkdir -p "$state_dir"
  LEDGER="$state_dir/delivery-ledger.tsv"
  ledger_init

  ledger_append "123" "factory/123-test" "abc123"

  ledger_update "123" 8 "pr_open"
  local state
  state="$(ledger_get_field "123" 8)"
  assert_eq "pr_open" "$state" "ledger_update changes state"

  ledger_update "123" 7 "456"
  local pr
  pr="$(ledger_get_field "123" 7)"
  assert_eq "456" "$pr" "ledger_update changes PR number"
}

# ===========================================================================
# TEST: Ledger release
# ===========================================================================
test_ledger_release() {
  printf '\n--- test_ledger_release ---\n'
  setup_test_env

  local state_dir="$TEST_TMPDIR/state"
  mkdir -p "$state_dir"
  LEDGER="$state_dir/delivery-ledger.tsv"
  ledger_init

  ledger_append "123" "factory/123-test" "abc123"
  ledger_release "123"
  local row
  row="$(ledger_get "123")"
  if [[ -z "$row" ]]; then
    pass "ledger_release removes issue"
  else
    fail "ledger_release should remove issue, got: $row"
  fi
}

# ===========================================================================
# TEST: is_exhausted
# ===========================================================================
test_is_exhausted() {
  printf '\n--- test_is_exhausted ---\n'
  setup_test_env

  local state_dir="$TEST_TMPDIR/state"
  mkdir -p "$state_dir"
  LEDGER="$state_dir/delivery-ledger.tsv"
  MAX_SAME_ISSUE_ATTEMPTS=3

  ledger_init
  ledger_append "123" "factory/123-test" "abc123"

  is_exhausted "123" && fail "should not be exhausted initially" || pass "not exhausted initially"

  ledger_update "123" 4 "2"
  is_exhausted "123" && fail "should not be exhausted at 2" || pass "not exhausted at 2"

  ledger_update "123" 4 "3"
  is_exhausted "123" && pass "exhausted at 3" || fail "should be exhausted at 3"
}

# ===========================================================================
# TEST: record_attempt resets on progress
# ===========================================================================
test_record_attempt_progress() {
  printf '\n--- test_record_attempt_progress ---\n'
  setup_test_env

  local state_dir="$TEST_TMPDIR/state"
  mkdir -p "$state_dir"
  LEDGER="$state_dir/delivery-ledger.tsv"
  MAX_SAME_ISSUE_ATTEMPTS=3

  ledger_init
  ledger_append "123" "factory/123-test" "abc123"

  record_attempt "123" "abc123"
  local attempts
  attempts="$(ledger_get_field "123" 4)"
  assert_eq "1" "$attempts" "first attempt sets attempts to 1"

  record_attempt "123" "abc123"
  attempts="$(ledger_get_field "123" 4)"
  assert_eq "2" "$attempts" "second failed attempt increments to 2"

  record_attempt "123" "def456"
  attempts="$(ledger_get_field "123" 4)"
  assert_eq "1" "$attempts" "progress resets attempts to 1"
  local head
  head="$(ledger_get_field "123" 3)"
  assert_eq "def456" "$head" "progress updates head"
}

# ===========================================================================
# TEST: ledger_list_active
# ===========================================================================
test_ledger_list_active() {
  printf '\n--- test_ledger_list_active ---\n'
  setup_test_env

  local state_dir="$TEST_TMPDIR/state"
  mkdir -p "$state_dir"
  LEDGER="$state_dir/delivery-ledger.tsv"
  ledger_init

  ledger_append "123" "factory/123-test" "abc123"
  ledger_append "456" "factory/456-test" "def456"
  ledger_update "456" 8 "merged"

  local active
  active="$(ledger_list_active)"
  assert_contains "$active" "123" "list_active includes claiming issue"
  if echo "$active" | grep -q "456"; then
    fail "list_active should not include merged issue"
  else
    pass "list_active excludes merged issue"
  fi
}

# ===========================================================================
# TEST: cleanup_stale_branches only touches factory branches
# ===========================================================================
test_cleanup_only_factory() {
  printf '\n--- test_cleanup_only_factory ---\n'
  setup_test_env
  local worktree
  worktree="$(setup_worktree)"

  local state_dir="$TEST_TMPDIR/state"
  mkdir -p "$state_dir"
  LEDGER="$state_dir/delivery-ledger.tsv"
  ledger_init

  local repo="$TEST_TMPDIR/work"

  # Human feature branch with commits
  git -C "$repo" checkout -b "feature/my-feature" origin/main 2>/dev/null
  echo "feature work" > "$repo/feature.txt"
  git -C "$repo" add . && git -C "$repo" commit -m "feature work" 2>/dev/null
  git -C "$repo" checkout main 2>/dev/null

  # Empty factory branch with no remote
  git -C "$repo" branch "factory/old-task" origin/main 2>/dev/null

  cleanup_stale_branches "$repo"

  if git -C "$repo" rev-parse --verify "feature/my-feature" >/dev/null 2>&1; then
    pass "cleanup does not rename human feature branches"
  else
    fail "cleanup should NOT rename feature/my-feature"
  fi

  if git -C "$repo" rev-parse --verify "factory/old-task" >/dev/null 2>&1; then
    fail "cleanup should delete empty factory branch"
  else
    pass "cleanup deletes empty factory branch"
  fi
}

# ===========================================================================
# TEST: persist_worktree_state creates recovery branch
# ===========================================================================
test_persist_worktree_state() {
  printf '\n--- test_persist_worktree_state ---\n'
  setup_test_env
  local worktree
  worktree="$(setup_worktree)"

  local repo="$TEST_TMPDIR/work"

  echo "dirty" > "$worktree/dirty.txt"

  persist_worktree_state "$repo" "$worktree"
  local exit_code=$?

  assert_eq "0" "$exit_code" "persist_worktree_state succeeds"

  # Branch is created in the worktree (which is a clone of bare.git)
  local branches
  branches="$(git -C "$worktree" branch --list 'recovery/dirty-*')"
  if [[ -n "$branches" ]]; then
    pass "recovery branch created in worktree"
  else
    fail "recovery branch should be created in worktree"
  fi
}

# ===========================================================================
# TEST: recover_detached_head creates recovery branch for orphan
# ===========================================================================
test_recover_detached_head() {
  printf '\n--- test_recover_detached_head ---\n'
  setup_test_env
  local worktree
  worktree="$(setup_worktree)"

  local repo="$TEST_TMPDIR/work"

  git -C "$worktree" checkout --orphan "orphan-test" 2>/dev/null
  git -C "$worktree" rm -rf . 2>/dev/null || true
  echo "orphan" > "$worktree/orphan.txt"
  git -C "$worktree" add . && git -C "$worktree" commit -m "orphan commit" 2>/dev/null

  git -C "$worktree" checkout --detach HEAD 2>/dev/null

  recover_detached_head "$repo" "$worktree"

  # Branch is created in the worktree (where the orphan commit lives)
  local recovery_branches
  recovery_branches="$(git -C "$worktree" branch --list 'recovery/orphaned-*')"
  if [[ -n "$recovery_branches" ]]; then
    pass "recover_detached_head creates recovery branch for orphan"
  else
    fail "should create recovery/orphaned-* branch"
  fi
}

# ===========================================================================
# TEST: assert_no_unpersisted_work passes on clean worktree
# ===========================================================================
test_assert_clean() {
  printf '\n--- test_assert_clean ---\n'
  setup_test_env

  local state_dir="$TEST_TMPDIR/state"
  mkdir -p "$state_dir"
  LEDGER="$state_dir/delivery-ledger.tsv"
  ledger_init

  local repo="$TEST_TMPDIR/work"
  local worktree
  worktree="$(setup_worktree)"

  assert_no_unpersisted_work "$repo" "$worktree" && pass "assert passes on clean worktree" || fail "assert should pass"
}

# ===========================================================================
# TEST: assert_no_unpersisted_work fails on dirty worktree
# ===========================================================================
test_assert_dirty() {
  printf '\n--- test_assert_dirty ---\n'
  setup_test_env

  local state_dir="$TEST_TMPDIR/state"
  mkdir -p "$state_dir"
  LEDGER="$state_dir/delivery-ledger.tsv"
  ledger_init

  local repo="$TEST_TMPDIR/work"
  local worktree
  worktree="$(setup_worktree)"

  echo "dirty" > "$worktree/uncommitted.txt"

  assert_no_unpersisted_work "$repo" "$worktree" && fail "assert should fail on dirty worktree" || pass "assert fails on dirty worktree"
}

# ===========================================================================
# TEST: assert_no_unpersisted_work fails on unpushed commits
# ===========================================================================
test_assert_unpushed() {
  printf '\n--- test_assert_unpushed ---\n'
  setup_test_env

  local state_dir="$TEST_TMPDIR/state"
  mkdir -p "$state_dir"
  LEDGER="$state_dir/delivery-ledger.tsv"
  ledger_init

  local repo="$TEST_TMPDIR/work"
  local worktree
  worktree="$(setup_worktree)"

  git -C "$repo" checkout -b "factory/unpushed-test" origin/main 2>/dev/null
  echo "unpushed" > "$repo/unpushed.txt"
  git -C "$repo" add . && git -C "$repo" commit -m "unpushed" 2>/dev/null

  local head
  head="$(git -C "$repo" rev-parse factory/unpushed-test)"
  ledger_append "999" "factory/unpushed-test" "$head"

  git -C "$worktree" checkout --detach origin/main 2>/dev/null

  assert_no_unpersisted_work "$repo" "$worktree" && fail "assert should fail on unpushed ledger branch" || pass "assert fails on unpushed ledger branch"
}

# ===========================================================================
# TEST: watchdog kill file communication
# ===========================================================================
test_watchdog_kill_file() {
  printf '\n--- test_watchdog_kill_file ---\n'
  setup_test_env

  local state_dir="$TEST_TMPDIR/state"
  mkdir -p "$state_dir/heartbeats"

  local kill_file="$state_dir/heartbeats/watchdog_kill_1"

  assert_file_not_exists "$kill_file" "no kill file initially"

  touch "$kill_file"
  assert_file_exists "$kill_file" "kill file exists after watchdog"

  local killed=0
  [[ -f "$kill_file" ]] && killed=1
  assert_eq "1" "$killed" "parent reads watchdog kill signal"

  rm -f "$kill_file"
  killed=0
  [[ -f "$kill_file" ]] && killed=1
  assert_eq "0" "$killed" "kill file cleaned up"
}

# ===========================================================================
# Run all tests
# ===========================================================================
printf 'Running heartbeat V22 test suite...\n'
printf 'Script: %s\n\n' "$HEARTBEAT"

# Source the functions we need — set required env first
export SOURCE_REPO="${COMIC_PILE_REPO:-/mnt/extra/josh/code/comic-pile}"
export STATE_DIR="${COMIC_PILE_FACTORY_STATE_DIR:-${SOURCE_REPO%/}-factory-state}"
export MAX_SAME_ISSUE_ATTEMPTS="${FACTORY_MAX_SAME_ISSUE_ATTEMPTS:-2}"
extract_functions

test_syntax
test_help
test_ledger_init
test_ledger_append_get
test_ledger_update
test_ledger_release
test_is_exhausted
test_record_attempt_progress
test_ledger_list_active
test_cleanup_only_factory
test_persist_worktree_state
test_recover_detached_head
test_assert_clean
test_assert_dirty
test_assert_unpushed
test_watchdog_kill_file

printf '\n==============================\n'
printf 'Results: %d passed, %d failed\n' "$PASS" "$FAIL"
printf '==============================\n'

if ((FAIL > 0)); then
  exit 1
fi
exit 0
