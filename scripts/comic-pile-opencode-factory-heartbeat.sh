#!/usr/bin/env bash
set -Eeuo pipefail

SOURCE_REPO="${COMIC_PILE_REPO:-/mnt/extra/josh/code/comic-pile}"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
WORKTREE="${COMIC_PILE_FACTORY_WORKTREE:-}"
STATE_DIR="${COMIC_PILE_FACTORY_STATE_DIR:-${SOURCE_REPO%/}-factory-state}"
MANIFEST_HELPER="$SCRIPT_DIR/opencode-model-manifest.sh"
DEFAULT_MODEL="${COMIC_PILE_DEFAULT_MODEL:-deepseek/deepseek-v4-flash}"
MODEL=""
AGENT="${OPENCODE_AGENT:-}"
USE_AUTO="${OPENCODE_AUTO:-1}"
HEARTBEAT_TIMEOUT="${FACTORY_HEARTBEAT_TIMEOUT:-60}"
IDLE_SECONDS="${FACTORY_IDLE_SECONDS:-10}"
FAILURE_BACKOFF_SECONDS="${FACTORY_FAILURE_BACKOFF_SECONDS:-0}"
MAX_FAILURES="${FACTORY_MAX_FAILURES:-2}"
MAX_SAME_ISSUE_ATTEMPTS="${FACTORY_MAX_SAME_ISSUE_ATTEMPTS:-2}"
WAIT_FOR_SCOUT="${COMIC_PILE_FACTORY_WAIT_FOR_SCOUT:-0}"
SCOUT_READY_FILE="${COMIC_PILE_FACTORY_SCOUT_READY_FILE:-$STATE_DIR/scout-initial-pass.done}"
SCOUT_PID_FILE="${COMIC_PILE_FACTORY_SCOUT_PID_FILE:-}"
WORKER_ID="${OPENCODE_FACTORY_WORKER_ID:-local-opencode-${HOSTNAME:-host}}"
WORKER_ID="${WORKER_ID//[^a-zA-Z0-9._-]/-}"
MODE="drain"
RUN_ONCE=0

usage() {
  cat <<'USAGE'
Usage: comic-pile-opencode-factory.sh [options]

Options:
  --watch             Stay alive and poll while idle or waiting on CI.
  --once              Run exactly one factory heartbeat.
  --repo PATH         Source comic-pile repository.
  --worktree PATH     Dedicated factory worktree.
  --state-dir PATH    Factory state directory (manifest, heartbeats, scout).
  --model ID          OpenCode model id (default: rotate confirmed models).
  --agent NAME        Optional OpenCode agent name.
  --idle-seconds N    Watch-mode idle poll interval.
  --worker-id ID      Durable GitHub lease identity.
  -h, --help          Show this help.
USAGE
}

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

is_nonnegative_integer() {
  [[ "$1" =~ ^[0-9]+$ ]]
}

while (($#)); do
  case "$1" in
    --watch) MODE="watch"; shift ;;
    --once) RUN_ONCE=1; shift ;;
    --repo) (($# >= 2)) || die "--repo requires a path"; SOURCE_REPO="$2"; shift 2 ;;
    --worktree) (($# >= 2)) || die "--worktree requires a path"; WORKTREE="$2"; shift 2 ;;
    --state-dir) (($# >= 2)) || die "--state-dir requires a path"; STATE_DIR="$2"; shift 2 ;;
    --model) (($# >= 2)) || die "--model requires an id"; MODEL="$2"; shift 2 ;;
    --agent) (($# >= 2)) || die "--agent requires a name"; AGENT="$2"; shift 2 ;;
    --idle-seconds) (($# >= 2)) || die "--idle-seconds requires a number"; IDLE_SECONDS="$2"; shift 2 ;;
    --worker-id)
      (($# >= 2)) || die "--worker-id requires a value"
      WORKER_ID="$2"
      WORKER_ID="${WORKER_ID//[^a-zA-Z0-9._-]/-}"
      shift 2
      ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown option: $1" ;;
  esac
done

[[ -n "$WORKTREE" ]] || WORKTREE="${SOURCE_REPO%/}-factory"
is_nonnegative_integer "$IDLE_SECONDS" || die "FACTORY_IDLE_SECONDS must be an integer"
is_nonnegative_integer "$FAILURE_BACKOFF_SECONDS" || die "FACTORY_FAILURE_BACKOFF_SECONDS must be an integer"
is_nonnegative_integer "$MAX_FAILURES" || die "FACTORY_MAX_FAILURES must be an integer"
((MAX_FAILURES >= 1)) || die "FACTORY_MAX_FAILURES must be at least 1"
[[ "$HEARTBEAT_TIMEOUT" =~ ^[1-9][0-9]*$ ]] || die "FACTORY_HEARTBEAT_TIMEOUT must be a positive integer"

for command in git gh opencode flock tee grep date sleep stat tail touch setsid; do
  command -v "$command" >/dev/null 2>&1 || die "required command not found: $command"
done

# ===========================================================================
# DELIVERY LEDGER
# ===========================================================================
# The ledger is the single source of truth for what the factory owns.
# Recovery operates on ledger records, not heuristic branch scanning.
#
# Format: TSV with header
# issue	branch	head	attempts	progress_heads	last_progress_at	pr_number	state
#
# States: claiming -> implementing -> pushed -> pr_open -> merged -> released
#
# The outer flock($STATE_DIR/factory.lock) serializes all local factory
# processes.  No finer-grained locking is needed today; if parallel local
# workers are ever introduced, this TSV must gain per-ledger atomic CAS.
# ===========================================================================
LEDGER="$STATE_DIR/delivery-ledger.tsv"
mkdir -p "$STATE_DIR"

ledger_init() {
  if [[ ! -f "$LEDGER" ]]; then
    printf 'issue\tbranch\thead\tattempts\tprogress_heads\tlast_progress_at\tpr_number\tstate\n' > "$LEDGER"
  fi
}

ledger_has_header() {
  head -1 "$LEDGER" 2>/dev/null | grep -q '^issue'
}

if ! ledger_has_header; then
  # Migrate old claimed-issues.txt if it exists
  if [[ -f "$STATE_DIR/claimed-issues.txt" ]]; then
    printf 'issue\tbranch\thead\tattempts\tprogress_heads\tlast_progress_at\tpr_number\tstate\n' > "$LEDGER"
    while IFS= read -r issue; do
      [[ -z "$issue" ]] && continue
      printf '%s\t\t\t0\t\t\t\tclaiming\n' "$issue" >> "$LEDGER"
    done < "$STATE_DIR/claimed-issues.txt"
    mv "$STATE_DIR/claimed-issues.txt" "$STATE_DIR/claimed-issues.txt.bak"
    printf '[ledger] Migrated claimed-issues.txt to delivery-ledger.tsv\n' >&2
  fi
fi
ledger_init

# Ledger helpers — field indices (1-based for awk)
# 1=issue 2=branch 3=head 4=attempts 5=progress_heads 6=last_progress_at 7=pr_number 8=state
ledger_get() {
  local issue="$1"
  awk -F'\t' -v issue="$issue" 'NR>1 && $1==issue' "$LEDGER"
}

ledger_get_field() {
  local issue="$1" field="$2"
  ledger_get "$issue" | awk -F'\t' -v f="$field" '{print $f}'
}

ledger_update() {
  local issue="$1" field="$2" value="$3"
  local tmp="$LEDGER.tmp"
  awk -F'\t' -v issue="$issue" -v field="$field" -v value="$value" '
    BEGIN {OFS="\t"}
    NR==1 {print; next}
    $1==issue {$(field)=value}
    {print}
  ' "$LEDGER" > "$tmp"
  mv "$tmp" "$LEDGER"
}

ledger_append() {
  local issue="$1" branch="$2" head="$3"
  printf '%s\t%s\t%s\t0\t\t\t\tclaiming\n' "$issue" "$branch" "$head" >> "$LEDGER"
}

ledger_release() {
  local issue="$1"
  local tmp="$LEDGER.tmp"
  awk -F'\t' -v issue="$issue" '$1!=issue || NR==1' "$LEDGER" > "$tmp"
  mv "$tmp" "$LEDGER"
}

ledger_list_active() {
  awk -F'\t' 'NR>1 && $8!="released" && $8!="merged"' "$LEDGER"
}

# ===========================================================================
# CLAIM TRACKING — wrapper-owned, not prompt-owned
# ===========================================================================
# Returns 0 if issue is already claimed (active in ledger), 1 if not.
is_claimed() {
  local issue="$1"
  local state
  state="$(ledger_get_field "$issue" 8)"
  [[ -n "$state" && "$state" != "released" && "$state" != "merged" ]]
}

# Returns 0 if this issue has been attempted too many times without progress.
is_exhausted() {
  local issue="$1"
  local attempts
  attempts="$(ledger_get_field "$issue" 4)"
  attempts="${attempts:-0}"
  ((attempts >= MAX_SAME_ISSUE_ATTEMPTS))
}

# Record that an attempt was made. If head changed, reset attempt counter.
record_attempt() {
  local issue="$1" new_head="$2"
  local old_head
  old_head="$(ledger_get_field "$issue" 3)"
  local attempts
  attempts="$(ledger_get_field "$issue" 4)"
  attempts="${attempts:-0}"

  if [[ "$new_head" != "$old_head" && -n "$old_head" ]]; then
    # Progress was made — reset attempt counter
    ledger_update "$issue" 4 1
    ledger_update "$issue" 3 "$new_head"
  else
    # No progress — increment
    ledger_update "$issue" 4 $((attempts + 1))
  fi
  ledger_update "$issue" 6 "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}

# Post-run bookkeeping: scan log for issue claims and update the ledger.
# Called after every successful heartbeat. Detects:
# - PR creation (gh pr create) — update state to pr_open, record PR number
# - Merge (gh pr merge) — update state to merged, release claim
# - Branch pushes — record attempt
post_run_bookkeeping() {
  local log_file="$1"

  # Detect PR creation and record PR number
  while IFS= read -r line; do
    local pr_num branch
    pr_num="$(printf '%s' "$line" | grep -oP 'https://github\.com/.*?/pull/\K[0-9]+' | head -1)"
    if [[ -n "$pr_num" ]]; then
      # Find which branch this PR is for from the log context
      branch="$(printf '%s' "$line" | grep -oP 'factory/\S+' | head -1)"
      if [[ -n "$branch" ]]; then
        local issue
        issue="$(awk -F'\t' -v b="$branch" 'NR>1 && $2==b {print $1}' "$LEDGER" 2>/dev/null | head -1)"
        if [[ -n "$issue" ]]; then
          ledger_update "$issue" 7 "$pr_num"
          ledger_update "$issue" 8 "pr_open"
          printf '[bookkeeping] Issue #%s: PR #%s opened on branch %s\n' "$issue" "$pr_num" "$branch" >&2
        fi
      fi
    fi
  done < <(grep -E 'https://github\.com/.*/pull/[0-9]+' "$log_file" 2>/dev/null)

  # Detect merges — release claims
  while IFS= read -r line; do
    local pr_num
    pr_num="$(printf '%s' "$line" | grep -oP 'https://github\.com/.*?/pull/\K[0-9]+' | head -1)"
    if [[ -n "$pr_num" ]]; then
      local issue
      issue="$(awk -F'\t' -v p="$pr_num" 'NR>1 && $7==p {print $1}' "$LEDGER" 2>/dev/null | head -1)"
      if [[ -n "$issue" ]]; then
        ledger_update "$issue" 8 "merged"
        printf '[bookkeeping] Issue #%s: PR #%s merged — releasing claim\n' "$issue" "$pr_num" >&2
      fi
    fi
  done < <(grep -iE '(merge|merged|successfully merged)' "$log_file" 2>/dev/null)

  # Record attempts for any ledger branches that were pushed
  while IFS=$'\t' read -r issue branch head _attempts _pa _lat _pr state; do
    [[ -z "$branch" || -z "$head" ]] && continue
    [[ "$state" == "released" || "$state" == "merged" ]] && continue
    # Check if this branch appears in the log as pushed
    if grep -qF "factory/$branch" "$log_file" 2>/dev/null || grep -qF "$branch" "$log_file" 2>/dev/null; then
      local current_head
      current_head="$(git -C "$SOURCE_REPO" rev-parse "$branch" 2>/dev/null || true)"
      if [[ -n "$current_head" ]]; then
        record_attempt "$issue" "$current_head"
      fi
    fi
  done < <(awk -F'\t' 'NR>1' "$LEDGER" 2>/dev/null)
}

# ===========================================================================
# RECOVERY — operates on ledger records + worktree inspection
# ===========================================================================

# Build set of branches checked out in other worktrees.
# Returns short branch names (e.g. "main", "factory/foo") for exact matching.
_checked_out_refs() {
  git -C "$SOURCE_REPO" worktree list --porcelain 2>/dev/null \
    | awk '/^branch /{sub("refs/heads/","",$2); print $2}' | sort -u
}

# Recover detached HEAD commits — create a recovery branch if HEAD is orphaned.
recover_detached_head() {
  local repo="$1"
  local worktree="$2"
  local head
  head="$(git -C "$worktree" rev-parse HEAD 2>/dev/null)" || return 0

  # Check if HEAD is reachable from any local or remote branch
  local reachable=0
  if git -C "$repo" branch --contains "$head" 2>/dev/null | grep -q .; then
    reachable=1
  fi
  if git -C "$repo" branch -r --contains "$head" 2>/dev/null | grep -q .; then
    reachable=1
  fi

  if ((reachable == 0)); then
    # HEAD is orphaned — create a recovery branch in the worktree where the commit exists
    local recovery_ref="recovery/orphaned-$(date +%Y%m%d%H%M%S)-${head:0:8}"
    printf '[recovery] Orphaned detached HEAD %s — creating %s\n' "${head:0:10}" "$recovery_ref" >&2
    git -C "$worktree" branch "$recovery_ref" "$head" 2>/dev/null || \
      git -C "$repo" branch "$recovery_ref" "$head" 2>/dev/null || true
    # Push it so it survives worktree reset
    git -C "$repo" push -u origin "$recovery_ref" 2>/dev/null || \
      git -C "$worktree" push -u origin "$recovery_ref" 2>/dev/null || \
      printf '[recovery] WARNING: could not push %s\n' "$recovery_ref" >&2
  fi
}

# Persist dirty worktree state — commit to a recovery branch, don't stash.
persist_worktree_state() {
  local repo="$1"
  local worktree="$2"

  # Check for any changes
  local dirty=0
  if ! git -C "$worktree" diff --quiet 2>/dev/null; then dirty=1; fi
  if ! git -C "$worktree" diff --cached --quiet 2>/dev/null; then dirty=1; fi
  if [[ -n "$(git -C "$worktree" status --porcelain 2>/dev/null)" ]]; then dirty=1; fi

  if ((dirty == 0)); then
    return 0
  fi

  local recovery_branch="recovery/dirty-$(date +%Y%m%d%H%M%S)"
  printf '[recovery] Dirty worktree — committing to %s\n' "$recovery_branch" >&2

  # Atomic: create AND switch to the recovery branch in one step.
  # If we cannot switch the worktree, abort — we must not commit on a
  # detached HEAD that may be overwritten by the next reset.
  if ! git -C "$worktree" checkout -b "$recovery_branch" 2>/dev/null; then
    printf '[recovery] FATAL: cannot create/switch to %s in worktree\n' "$recovery_branch" >&2
    return 1
  fi

  git -C "$worktree" add -A 2>/dev/null
  git -C "$worktree" commit -m "factory: recover interrupted work $(date -u +%Y-%m-%dT%H:%M:%SZ)" 2>/dev/null || true

  # Push so it survives reset
  git -C "$repo" push -u origin "$recovery_branch" 2>/dev/null || \
    printf '[recovery] WARNING: could not push %s\n' "$recovery_branch" >&2

  # Return to detached HEAD
  git -C "$worktree" checkout --detach HEAD 2>/dev/null || true
}

# Push unpushed ledger branches.
push_ledger_branches() {
  local repo="$1"
  local pushed=0

  while IFS=$'\t' read -r issue branch head attempts _pa _lat _pr state; do
    [[ -z "$branch" || -z "$head" ]] && continue
    [[ "$state" == "released" || "$state" == "merged" ]] && continue

    # Check if branch exists locally
    if ! git -C "$repo" rev-parse --verify "refs/heads/$branch" >/dev/null 2>&1; then
      printf '[recovery] Ledger branch %s does not exist locally; skipping.\n' "$branch" >&2
      continue
    fi

    # Check if branch is ahead of remote
    local ahead=0
    if git -C "$repo" rev-parse --verify "origin/$branch" >/dev/null 2>&1; then
      ahead="$(git -C "$repo" rev-list --count "origin/$branch..$branch" 2>/dev/null || printf 0)"
    else
      # No remote — count commits ahead of main
      ahead="$(git -C "$repo" rev-list --count "origin/main..$branch" 2>/dev/null || printf 0)"
    fi

    if ((ahead > 0)); then
      printf '[recovery] Pushing ledger branch %s (%d unpushed commit(s))\n' "$branch" "$ahead" >&2
      if git -C "$repo" push -u origin "$branch" 2>&1; then
        pushed=$((pushed + ahead))
        # Update ledger head to the branch's actual current head
        local new_head
        new_head="$(git -C "$repo" rev-parse "$branch" 2>/dev/null)"
        if [[ -n "$new_head" ]]; then
          ledger_update "$issue" 3 "$new_head"
        fi
        ledger_update "$issue" 8 "pushed"
      else
        printf '[recovery] WARNING: push failed for %s\n' "$branch" >&2
      fi
    fi
  done < <(awk -F'\t' 'NR>1' "$LEDGER" 2>/dev/null)

  printf '[recovery] Pushed %d commit(s) from ledger branches.\n' "$pushed" >&2
}

# Delete empty factory branches not in ledger and not checked out elsewhere.
# Only touches branches owned by the factory (factory/ prefix, or in the ledger).
# Never renames or deletes human feature branches.
cleanup_stale_branches() {
  local repo="$1"
  local checked_out
  checked_out="$(_checked_out_refs)"

  while IFS= read -r branch; do
    [[ -z "$branch" ]] && continue
    # Never touch main, release/*, or branches checked out elsewhere
    [[ "$branch" == "main" || "$branch" == "master" ]] && continue
    [[ "$branch" == release/* ]] && continue
    [[ "$branch" == recovery/* ]] && continue
    echo "$checked_out" | grep -qx "$branch" && continue

    # Only touch factory-owned branches: factory/* prefix, or explicitly in the ledger
    local is_factory=0
    [[ "$branch" == factory/* ]] && is_factory=1
    if awk -F'\t' -v b="$branch" 'NR>1 && $2==b' "$LEDGER" | grep -q .; then
      is_factory=1
    fi

    if ((is_factory == 0)); then
      continue  # Not factory-owned — never touch
    fi

    # Check if it has a remote or PR
    if git -C "$repo" rev-parse --verify "origin/$branch" >/dev/null 2>&1; then
      continue  # Has remote — don't touch
    fi

    # Check if it has unique commits
    local ahead
    ahead="$(git -C "$repo" rev-list --count "origin/main..$branch" 2>/dev/null || printf 0)"
    if ((ahead > 0)); then
      # Has commits but no remote — push to recovery namespace (sanitized name)
      local safe_branch="${branch//\//-}"
      local recovery="recovery/untracked-$(date +%Y%m%d%H%M%S)-${safe_branch}"
      printf '[recovery] Factory branch %s has %d commit(s) — renaming to %s\n' "$branch" "$ahead" "$recovery" >&2
      git -C "$repo" branch -m "$branch" "$recovery" 2>/dev/null || continue
      git -C "$repo" push -u origin "$recovery" 2>/dev/null || \
        printf '[recovery] WARNING: could not push %s\n' "$recovery" >&2
    else
      # Empty, no remote, not in ledger — safe to delete
      printf '[recovery] Deleting empty factory branch %s\n' "$branch" >&2
      git -C "$repo" branch -D "$branch" 2>/dev/null || true
    fi
  done < <(git -C "$repo" branch --format='%(refname:short)' 2>/dev/null)
}

# Full recovery pass — ledger-driven, worktree-aware.
run_recovery() {
  local repo="$1"
  local worktree="$2"

  printf '[recovery] Starting recovery pass...\n' >&2

  # 1. Recover detached HEAD commits in the worktree
  recover_detached_head "$repo" "$worktree"

  # 2. Persist dirty worktree state
  persist_worktree_state "$repo" "$worktree"

  # 3. Push all unpushed ledger branches
  push_ledger_branches "$repo"

  # 4. Clean up stale non-ledger branches
  cleanup_stale_branches "$repo"

  printf '[recovery] Recovery pass complete.\n' >&2
}

# ===========================================================================
# ASSERT NO UNPERSISTED WORK — reset is forbidden until this passes
# ===========================================================================
assert_no_unpersisted_work() {
  local repo="$1"
  local worktree="$2"
  local failures=0

  # 1. Worktree must be clean (no uncommitted, no untracked)
  if [[ -n "$(git -C "$worktree" status --porcelain 2>/dev/null)" ]]; then
    printf '[assert] FAIL: worktree has uncommitted/untracked files\n' >&2
    git -C "$worktree" status --short >&2
    failures=$((failures + 1))
  fi

  # 2. HEAD must be reachable from a durable ref
  local head
  head="$(git -C "$worktree" rev-parse HEAD 2>/dev/null)" || true
  if [[ -n "$head" ]]; then
    local reachable=0
    if git -C "$repo" branch --contains "$head" 2>/dev/null | grep -q .; then
      reachable=1
    fi
    if git -C "$repo" branch -r --contains "$head" 2>/dev/null | grep -q .; then
      reachable=1
    fi
    if ((reachable == 0)); then
      printf '[assert] FAIL: HEAD %s is not reachable from any branch\n' "${head:0:10}" >&2
      failures=$((failures + 1))
    fi
  fi

  # 3. Every active ledger entry must have its branch pushed
  while IFS=$'\t' read -r issue branch head attempts _pa _lat _pr state; do
    [[ -z "$branch" || -z "$head" ]] && continue
    [[ "$state" == "released" || "$state" == "merged" ]] && continue

    # Branch must exist locally
    if ! git -C "$repo" rev-parse --verify "refs/heads/$branch" >/dev/null 2>&1; then
      printf '[assert] FAIL: ledger branch %s does not exist locally\n' "$branch" >&2
      failures=$((failures + 1))
      continue
    fi

    # Branch must be pushed (remote head must equal or be ahead of local head)
    local remote_head=""
    if git -C "$repo" rev-parse --verify "origin/$branch" >/dev/null 2>&1; then
      remote_head="$(git -C "$repo" rev-parse "origin/$branch" 2>/dev/null)"
    fi
    if [[ -z "$remote_head" ]]; then
      printf '[assert] FAIL: ledger branch %s has no remote\n' "$branch" >&2
      failures=$((failures + 1))
    else
      # Local head must be an ancestor of (or equal to) the remote head
      # i.e. everything local is pushed
      if ! git -C "$repo" merge-base --is-ancestor "$branch_head" "$remote_head" 2>/dev/null; then
        local unpushed
        unpushed="$(git -C "$repo" rev-list --count "origin/$branch..$branch" 2>/dev/null || printf 0)"
        printf '[assert] FAIL: ledger branch %s has %d unpushed commit(s)\n' "$branch" "$unpushed" >&2
        failures=$((failures + 1))
      fi
    fi

    # Branch head must match ledger head (or be ahead of it)
    local branch_head
    branch_head="$(git -C "$repo" rev-parse "$branch" 2>/dev/null)"
    if [[ -n "$branch_head" && "$branch_head" != "$head" ]]; then
      # Check if branch contains the ledger head (branch moved forward)
      if ! git -C "$repo" merge-base --is-ancestor "$head" "$branch" 2>/dev/null; then
        printf '[assert] FAIL: ledger branch %s head %s does not contain expected %s\n' \
          "$branch" "${branch_head:0:10}" "${head:0:10}" >&2
        failures=$((failures + 1))
      fi
    fi
  done < <(awk -F'\t' 'NR>1' "$LEDGER" 2>/dev/null)

  if ((failures > 0)); then
    printf '[assert] BLOCKED: %d unpersisted work item(s) found. Reset forbidden.\n' "$failures" >&2
    return 1
  fi

  printf '[assert] PASS: all factory work is durable.\n' >&2
  return 0
}

# ===========================================================================
# IMMEDIATE RECOVERY ON ABNORMAL TERMINATION
# ===========================================================================
# Called when the watchdog kills the agent or the process exits non-zero.
# Persists work right now rather than waiting for the next heartbeat.
immediate_recovery() {
  local repo="$1"
  local worktree="$2"
  local reason="$3"

  printf '[immediate-recovery] Agent terminated (%s). Persisting work...\n' "$reason" >&2

  recover_detached_head "$repo" "$worktree"
  persist_worktree_state "$repo" "$worktree"

  # Push any branches that are now ahead
  while IFS=$'\t' read -r issue branch head attempts _pa _lat _pr state; do
    [[ -z "$branch" ]] && continue
    [[ "$state" == "released" || "$state" == "merged" ]] && continue

    if git -C "$repo" rev-parse --verify "refs/heads/$branch" >/dev/null 2>&1; then
      local ahead=0
      if git -C "$repo" rev-parse --verify "origin/$branch" >/dev/null 2>&1; then
        ahead="$(git -C "$repo" rev-list --count "origin/$branch..$branch" 2>/dev/null || printf 0)"
      else
        # No remote yet — count commits ahead of main
        ahead="$(git -C "$repo" rev-list --count "origin/main..$branch" 2>/dev/null || printf 0)"
      fi
      if ((ahead > 0)); then
        printf '[immediate-recovery] Pushing %s (%d commit(s))\n' "$branch" "$ahead" >&2
        git -C "$repo" push -u origin "$branch" 2>/dev/null || true
      fi
    fi
  done < <(awk -F'\t' 'NR>1' "$LEDGER" 2>/dev/null)

  printf '[immediate-recovery] Done.\n' >&2
}

# ===========================================================================
# PRE-FLIGHT
# ===========================================================================
git -C "$SOURCE_REPO" rev-parse --show-toplevel >/dev/null 2>&1 || die "not a Git repository: $SOURCE_REPO"
gh auth status >/dev/null 2>&1 || die "GitHub CLI is not authenticated; run: gh auth login"

[[ -x "$MANIFEST_HELPER" ]] || die "manifest helper is not executable: $MANIFEST_HELPER"
mkdir -p "$STATE_DIR"
"$MANIFEST_HELPER" init "$STATE_DIR"

if [[ "$WAIT_FOR_SCOUT" == "1" ]]; then
  printf 'Waiting for the model scout to complete its initial pass...\n'
  while [[ ! -f "$SCOUT_READY_FILE" ]]; do
    if [[ -n "$SCOUT_PID_FILE" && -f "$SCOUT_PID_FILE" ]]; then
      scout_pid="$(cat "$SCOUT_PID_FILE" 2>/dev/null || true)"
      if [[ "$scout_pid" =~ ^[0-9]+$ ]] && ! kill -0 -- "-$scout_pid" 2>/dev/null; then
        die "model scout exited before completing its initial pass"
      fi
    fi
    sleep 2
  done
  printf 'Model scout initial pass complete; using the confirmed-model manifest.\n'
fi

if [[ -z "$MODEL" ]]; then
  if [[ -n "${OPENCODE_MODEL:-}" ]]; then
    MODEL="$OPENCODE_MODEL"
  else
    MODEL="$("$MANIFEST_HELPER" next "$DEFAULT_MODEL" "$STATE_DIR")"
    printf 'Rotating to model from manifest: %s\n' "$MODEL" >&2
  fi
fi
export OPENCODE_MODEL="$MODEL"

if ! opencode models 2>/dev/null | grep -Fq "$MODEL"; then
  die "configured model was not found in opencode models: $MODEL"
fi

if ! git -C "$WORKTREE" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  printf 'Creating dedicated factory worktree: %s\n' "$WORKTREE"
  git -C "$SOURCE_REPO" fetch --prune origin
  mkdir -p "$(dirname "$WORKTREE")"
  git -C "$SOURCE_REPO" worktree add --detach "$WORKTREE" origin/main
fi

mkdir -p "$WORKTREE/.opencode_logs" "$WORKTREE/.opencode_handoff"

# ===========================================================================
# LOCK — shared state dir lock, not worktree lock
# ===========================================================================
exec 9>"$STATE_DIR/factory.lock"
flock -n 9 || die "another local factory process already holds the factory lock"

# ===========================================================================
# RECOVERY + ASSERT + RESET
# ===========================================================================
# Sequence must be atomic: fetch -> recover -> assert -> reset -> run agent.
# The lock is held throughout.
printf 'Fetching latest origin...\n'
git -C "$SOURCE_REPO" fetch --prune origin

printf 'Running recovery pass...\n'
run_recovery "$SOURCE_REPO" "$WORKTREE"

printf 'Asserting no unpersisted work...\n'
if ! assert_no_unpersisted_work "$SOURCE_REPO" "$WORKTREE"; then
  die "Cannot reset: unpersisted factory work exists. Fix manually or run recovery."
fi

printf 'Refreshing factory worktree to latest origin/main...\n'
git -C "$WORKTREE" switch -f --detach origin/main
printf 'Factory worktree is now at %s (%s).\n' \
  "$(git -C "$WORKTREE" rev-parse --short origin/main)" \
  "$(git -C "$WORKTREE" log -1 --format='%s' origin/main)"

# ===========================================================================
# FACTORY PROMPT
# ===========================================================================
FACTORY_PROMPT="$(cat <<'PROMPT'
Act as one high-ownership local factory heartbeat for JoshCLWren/comic-pile.
Durable worker ID: `__WORKER_ID__`.

Read current-main AGENTS.md, docs/ISSUE_EXECUTION_PROTOCOL.md, and
`docs/AUTONOMOUS_FACTORY_POLICY.md` before selection. The canonical factory policy wins
when any older or generic instruction conflicts.

MISSION
Drive the open issue backlog to zero. Success means issues truthfully closed and defects
removed, not PR count, comments, commits, reviews, labels, or hours spent.

CONTINUOUS CYCLE
1. Drain every executable issue except deferred backlog-zero checkpoint #679.
2. When #679 is the only remaining executable checkpoint, restore the complete maintained
   Chromium Playwright E2E suite.
3. Create one GitHub issue per independent reproducible Chromium product defect with failure
   evidence and the `bug` label.
4. Resume draining the replenished backlog immediately.
5. Preserve `user-reported` only for bugs actually reported by a user.

Firefox and WebKit are optional diagnostics for browser-specific investigations. They are
not required factory release coverage and must not delay issue closure or merges.

SELECTION ORDER
Before choosing work, enumerate current open PRs, current review threads, leases, and open
issues. Select exactly in this order:
1. A branch-caused failing check, conflict, or actionable review finding that currently
   prevents an active implementation PR from becoming mergeable.
2. The newest unclaimed open issue labeled both `user-reported` and `bug`.
3. The highest-priority unclaimed reproducible E2E-discovered `bug` issue.
4. The highest-value unclaimed executable issue, honoring priorities and dependencies while
   excluding #679 until every other executable issue is closed.
5. Existing PR work only when required to complete its issue contract or make it mergeable.
6. Factory maintenance only when factory behavior blocks issue delivery.

CONCURRENCY
At most one implementation worker owns an issue unless workers explicitly declare
non-overlapping file ownership. Once another worker holds the highest-priority issue, choose
the next eligible issue. When fewer than four substantive implementation PRs are open and
unclaimed executable issues exist, open a coherent implementation for a separate issue
instead of polishing an existing PR.

ANTI-ORBIT RULES
- Green, ready, review-passed, or merge-gated PRs are excluded from ordinary selection.
- Do not repeatedly debate, review, summarize, or embellish the same few PRs.
- Do not add optional tests, cleanup, documentation, PR-body edits, evidence prose, or minor
  slices while higher-priority executable issues remain.
- Waiting for CI, review, merge, Josh, or external availability does not reserve the worker.
  Preserve context and select another free issue.
- Do not create replacement PRs merely because main advanced.
- Release notes are post-merge infrastructure. Never create, repair, or gate implementation
  delivery on `docs/changelog.d` fragments or `/changelog.md`; the dedicated release writer
  publishes merged user-facing work to the database-backed release ledger and reconciliation
  owns missed records.
- Do not split one issue into avoidable foundation or stage PRs. Implement the full contract
  in one coherent non-draft PR whenever reasonably reviewable.

REVIEW FEEDBACK IS WORK
Before pass, ready, merge-gated, or any claim that no blocking correctness issue remains:
- fetch all current-SHA review submissions and inline review threads;
- ignore only status noise, summaries, release notes, rate-limit notices, and optional
  finishing-touch advertisements;
- classify every actionable finding as fixed, demonstrably outdated by a specific later
  change, or rebutted once with concrete evidence;
- respond to or resolve every actionable current thread;
- refuse readiness or merge while an unresolved actionable correctness, security,
  ownership, data-integrity, migration, concurrency, recovery, or test-validity finding
  remains.
Your own review conclusion never silently overrides existing human or bot feedback.
Every push invalidates all prior review and gate conclusions.

GATED MERGES ARE AUTHORIZED
You may merge without asking again only when every gate is true for the exact current head:
- PR is open, non-draft, mergeable, and conflict-free;
- all required CI checks completed successfully;
- all actionable current review findings are accounted for;
- focused validation or exact-head CI establishes the required evidence;
- declared PR scope is complete and truthful;
- merge is safe for ownership, migrations, deployment, security, and data;
- merge method is allowed by repository settings;
- pass the exact expected head SHA to the merge operation.
Never enable auto-merge. Never merge a moved SHA. If any gate cannot be verified, repair the
branch or select other work. After merge, verify issue closure and continue remaining issue
work if the issue did not truthfully close.

WORK LOOP
`inspect issue -> claim -> implement closure-critical behavior -> focused test -> commit ->
push -> open/update PR -> inspect exact SHA and CI -> inspect all review feedback -> repair ->
revalidate -> merge when gated -> verify issue closure`

**DELIVERY INVARIANTS — enforced by the wrapper, not just prompt instructions:**

1. **PUSH BEFORE ADVANCE.** After every `git commit`, immediately `git push` to a
   named branch. The wrapper's recovery pass will push orphaned work on the NEXT
   heartbeat, but if you commit and then reset without pushing, the recovery has
   extra work and you risk losing uncommitted changes.

2. **NO PLANNING-ONLY SUCCESS.** Writing `docs/issue-plans/...` does not count as
   implementation progress. A plan without code commits is a failed heartbeat.

3. **OPEN A PR EVERY TIME YOU PUSH.** After pushing a branch, immediately create or
   update a PR with `gh pr create` or `gh pr edit`. A branch without a PR is invisible
   to other factories and to Josh.

4. **BRANCH HYGIENE.** Every commit must land on a named branch (e.g.,
   `factory/<issue>-<slug>`). Never commit to a detached HEAD. Never leave empty
   branches behind.

5. **RESUME, DON'T RECREATE.** If you previously worked on an issue, resume the
   same branch. Do not create a fresh branch for the same issue. The wrapper tracks
   claims in the delivery ledger and will not let you exceed the attempt limit.

A normal heartbeat while executable work exists must push substantive code/tests/migration,
repair a blocking defect/conflict/review finding, open a coherent non-draft implementation
PR, perform a fully gated exact-head merge, create evidence-backed Chromium bug issues during
the backlog-zero phase, or repair factory code that blocks delivery. Comments, labels,
claims, reviews, PR-body edits, and ready markers alone do not count.

BACKLOG-ZERO CHROMIUM PHASE
Issue #679 is deferred and excluded from ordinary executable selection while any other
executable issue remains open. Once all other executable issues are closed, restore and run
the maintained Chromium suite, distinguish product failures from infrastructure failures,
create one focused `bug` issue per independent reproducible product defect, and resume
normal backlog draining. Firefox and WebKit are optional diagnostics, not completion gates.

REPOSITORY SAFETY
- Never push directly to main.
- Never create or convert a draft PR unless Josh explicitly requested it.
- Never enable auto-merge.
- Never weaken or bypass checks, remove meaningful coverage, use suppressions to fake green,
  or manufacture evidence.
- Never mutate schedules or factory topology.

GITHUB VISIBILITY
Maintain `factory`, exactly one workflow-stage label, and exactly one next-action owner label on
every managed issue and PR. Reconcile each transition with one full atomic label-set replacement;
never use sequential remove/add calls that expose contradictory intermediate states. This local
worker maps to `factory:local`. Labels are visibility metadata and never substitute for merge gates.

DURABLE RESUME PACKET V1
Before releasing ownership, reaching the runtime limit, switching work, or ending with a claimed
item unfinished, create or update one canonical GitHub comment in place:
`<!-- factory-resume:v1 -->`
`## Factory resume packet`
`Head: <current SHA or none>`
`Current hypothesis: <one or two concrete sentences>`
`Files touched: <paths, or none>`
`Checks: <passed and failed commands/checks; include the decisive failure>`
`Next narrow verification: <one specific command, inspection, or experiment>`
`Remaining blocker/action: <what the next worker must resolve>`
`Updated by: <durable worker ID and UTC timestamp>`
Keep it short, factual, secret-free, and explicit about local versus CI evidence. A takeover worker
must verify that the recorded head still matches and update or discard stale claims before acting.
The packet never substitutes for commits, tests, review markers, acceptance criteria, or labels.

TOOLING GATE
Confirm checkout, focused tests, commit, push, GitHub reads/writes, review-thread access,
CI-log access, and merge access. Print:
`FACTORY_TOOLING: local-checkout=yes focused-tests=yes ci-debug=yes commit=yes push=yes review-threads=yes merge=yes`
Never claim a command ran unless it ran.

LEASE MARKERS
Issue claim:
`<!-- comic-pile-factory-implement-claim-v3:issue-<n>:<worker-id>:<epoch>:attempt-<n> -->`
Issue progress:
`<!-- comic-pile-factory-implement-progress-v3:issue-<n>:<worker-id>:<epoch> -->`
Review claim:
`<!-- comic-pile-factory-review-claim-v2:<sha>:<worker-id>:<epoch> -->`
Verdicts:
`<!-- comic-pile-factory-review-v2:<sha>:pass -->`
`<!-- comic-pile-factory-review-v2:<sha>:changes-required -->`
Fix claim:
`<!-- comic-pile-factory-fix-claim-v3:<sha>:<worker-id>:<epoch>:attempt-<n> -->`
Fix progress:
`<!-- comic-pile-factory-fix-progress-v3:<sha>:<worker-id>:<epoch> -->`
Ready:
`<!-- comic-pile-factory-ready-v2:<sha> -->`
Needs human:
`<!-- comic-pile-factory-needs-human-v2:<sha-or-issue> -->`
Released:
`<!-- comic-pile-factory-claim-released-v3:<target>:<worker-id>:<epoch>:<reason> -->`

MODEL SIGNING
The model running this heartbeat is `__MODEL_ID__`. Sign PR bodies you open or update with:
`Model: __MODEL_ID__`

STOP CONDITIONS
Stop only when no executable work exists outside the deferred #679 Chromium cycle, Josh
redirects the work, a genuine human-only irreversible/product/credential decision is
required, all safe write paths fail, or an evidence-grounded repair ceiling is reached.

TERMINAL RESULT
The final line must be exactly one of:
FACTORY_RESULT: changed
FACTORY_RESULT: idle
FACTORY_RESULT: needs-human
PROMPT
)"

FACTORY_PROMPT="${FACTORY_PROMPT//__WORKER_ID__/$WORKER_ID}"
FACTORY_PROMPT="${FACTORY_PROMPT//__MODEL_ID__/$MODEL}"

printf 'ComicPile local factory v22 (delivery-ledger)\n'
printf '  Source repo: %s\n' "$SOURCE_REPO"
printf '  Worktree:    %s\n' "$WORKTREE"
printf '  Ledger:      %s\n' "$LEDGER"
printf '  Model:       %s\n' "$MODEL"
printf '  Agent:       %s\n' "${AGENT:-<default>}"
printf '  Mode:        %s\n' "$MODE"
printf '  Run once:    %s\n' "$([[ "$RUN_ONCE" == "1" ]] && printf yes || printf no)"
printf '  Worker ID:   %s\n' "$WORKER_ID"

report_counts() {
  local open_prs open_issues pending_campaign active_claims
  open_prs="$(gh pr list --repo JoshCLWren/comic-pile --state open --limit 1000 --json number --jq 'length' 2>/dev/null || printf '?')"
  open_issues="$(gh issue list --repo JoshCLWren/comic-pile --state open --limit 1000 --json number --jq 'length' 2>/dev/null || printf '?')"
  pending_campaign="$(gh issue list --repo JoshCLWren/comic-pile --state open --limit 1000 --label ralph-task --label ralph-status:pending --json number --jq 'length' 2>/dev/null || printf '?')"
  active_claims="$(awk -F'\t' 'NR>1 && $8!="released" && $8!="merged"' "$LEDGER" 2>/dev/null | wc -l)"
  printf '  GitHub queue: %s open PR(s), %s open issue(s), %s pending, %s ledger claim(s)\n' \
    "$open_prs" "$open_issues" "$pending_campaign" "$active_claims"
}

report_counts
consecutive_failures=0
heartbeat=0

while true; do
  heartbeat=$((heartbeat + 1))
  timestamp="$(date +%Y%m%d_%H%M%S)"
  log_file="$WORKTREE/.opencode_logs/factory_${timestamp}_heartbeat_${heartbeat}.log"
  printf '\n[%s] Starting heartbeat %d (model %s)\n' "$(date --iso-8601=seconds)" "$heartbeat" "$MODEL"

  opencode_args=(run -m "$MODEL" --dir "$WORKTREE" --title "ComicPile factory heartbeat ${timestamp}")
  [[ -n "$AGENT" ]] && opencode_args+=(--agent "$AGENT")
  [[ "$USE_AUTO" == "1" ]] && opencode_args+=(--auto)

  hb_file="$STATE_DIR/heartbeats/factory_heartbeat_${heartbeat}.hb"
  watchdog_kill_file="$STATE_DIR/heartbeats/watchdog_kill_${heartbeat}"
  mkdir -p "$STATE_DIR/heartbeats"
  touch "$hb_file"
  rm -f "$watchdog_kill_file"

  set +e
  setsid bash -c '
    LOG="$1"; HB="$2"; shift 2
    opencode "$@" 2>&1 | while IFS= read -r line; do
      touch "$HB"
      printf "%s\n" "$line"
    done | tee "$LOG"
    exit "${PIPESTATUS[0]}"
  ' _ "$log_file" "$hb_file" "${opencode_args[@]}" "$FACTORY_PROMPT" &
  run_pid=$!
  (
    while kill -0 "$run_pid" 2>/dev/null; do
      age=$(( $(date +%s) - $(stat -c %Y "$hb_file" 2>/dev/null || printf '%s' "$(date +%s)") ))
      if grep -Eiq 'Tokens per minute limit exceeded|too many tokens processed' "$log_file" 2>/dev/null; then
        printf 'WATCHDOG: stopping heartbeat %d immediately after token-per-minute limit.\n' "$heartbeat" >&2
        kill -TERM -- "-$run_pid" 2>/dev/null || kill -TERM "$run_pid" 2>/dev/null || true
        sleep 1
        kill -KILL -- "-$run_pid" 2>/dev/null || kill -KILL "$run_pid" 2>/dev/null || true
        touch "$watchdog_kill_file"
        break
      fi
      if ((age > HEARTBEAT_TIMEOUT)); then
        printf 'WATCHDOG: killing heartbeat %d run %s because no output arrived for %ss\n' "$heartbeat" "$run_pid" "$age" >&2
        kill -9 -- "-$run_pid" 2>/dev/null || kill -9 "$run_pid" 2>/dev/null || true
        touch "$watchdog_kill_file"
        break
      fi
      sleep 1
    done
  ) &
  watchdog_pid=$!
  wait "$run_pid"
  opencode_status=$?
  kill "$watchdog_pid" 2>/dev/null || true
  watchdog_killed=0
  [[ -f "$watchdog_kill_file" ]] && watchdog_killed=1
  rm -f "$watchdog_kill_file"
  set -e

  # =========================================================================
  # POST-RUN: immediate recovery if abnormal termination
  # =========================================================================
  if ((opencode_status != 0)) || ((watchdog_killed == 1)); then
    reason="exit=$opencode_status"
    ((watchdog_killed == 1)) && reason="watchdog-kill"
    immediate_recovery "$SOURCE_REPO" "$WORKTREE" "$reason"
    # Verify durability after recovery — hard-fail if persistence failed
    if ! assert_no_unpersisted_work "$SOURCE_REPO" "$WORKTREE"; then
      die "IMMEDIATE RECOVERY FAILED: cannot make work durable. Stopping rotation."
    fi
  fi

  if ((opencode_status == 0)); then
    if ! "$MANIFEST_HELPER" record "$MODEL" "$STATE_DIR" >/dev/null 2>&1; then
      consecutive_failures=$((consecutive_failures + 1))
      printf 'Heartbeat succeeded but failed to record model usage (%d/%d): %s\n' "$consecutive_failures" "$MAX_FAILURES" "$log_file" >&2
      ((consecutive_failures < MAX_FAILURES)) || die "stopping after $consecutive_failures consecutive OpenCode failures"
      sleep "$FAILURE_BACKOFF_SECONDS"
      continue
    fi
    post_run_bookkeeping "$log_file"
  fi

  if ((opencode_status != 0)); then
    consecutive_failures=$((consecutive_failures + 1))
    printf 'Heartbeat failed with status %d (%d/%d): %s\n' "$opencode_status" "$consecutive_failures" "$MAX_FAILURES" "$log_file" >&2
    ((consecutive_failures < MAX_FAILURES)) || die "stopping after $consecutive_failures consecutive OpenCode failures"
    sleep "$FAILURE_BACKOFF_SECONDS"
    continue
  fi

  if grep -Fq 'FACTORY_RESULT: needs-human' "$log_file"; then
    printf 'Factory needs human judgment. See %s\n' "$log_file" >&2
    exit 2
  fi

  if grep -Fq 'FACTORY_RESULT: changed' "$log_file"; then
    consecutive_failures=0
    report_counts
    if ((RUN_ONCE == 1)); then
      printf 'One-heartbeat mode completed after making progress.\n'
      exit 0
    fi
    printf 'Heartbeat made progress. Starting the next heartbeat immediately.\n'
    sleep 1
    continue
  fi

  if grep -Fq 'FACTORY_RESULT: idle' "$log_file"; then
    consecutive_failures=0
    report_counts
    if ((RUN_ONCE == 1)) || [[ "$MODE" == "drain" ]]; then
      printf 'Factory is currently idle. Stopping cleanly.\n'
      exit 0
    fi
    printf 'Factory is idle or waiting on CI. Checking again in %s seconds.\n' "$IDLE_SECONDS"
    sleep "$IDLE_SECONDS"
    continue
  fi

  consecutive_failures=$((consecutive_failures + 1))
  printf 'Heartbeat returned no terminal marker (%d/%d): %s\n' "$consecutive_failures" "$MAX_FAILURES" "$log_file" >&2
  ((consecutive_failures < MAX_FAILURES)) || die "stopping because OpenCode repeatedly omitted its terminal result marker"
  sleep "$FAILURE_BACKOFF_SECONDS"
done
