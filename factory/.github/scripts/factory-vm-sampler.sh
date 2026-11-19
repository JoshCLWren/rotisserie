#!/usr/bin/env bash
# Out-of-VM evidence for factory sessions whose hosted runner dies mid-step.
#
# When the VM is shut down, the step's console output never reaches the job
# log, so anything we print is lost. Every interval this pushes one small
# snapshot commit to a hidden ref (refs/factory-diag/<run_id>), which no
# workflow triggers on. A clean stop deletes the ref, so only runs whose VM
# died leave evidence behind.
#
# The repository is public: snapshots carry only resource counters, process
# names (never arguments), and kernel OOM/kill or shutdown lines. Never add
# agent output, environment, or command lines here.
set -uo pipefail

run_id="${1:?run id required}"
interval="${2:-15}"
workspace="${GITHUB_WORKSPACE:?GITHUB_WORKSPACE is required}"
ref="refs/factory-diag/${run_id}"

# Resolve the push URL once; it embeds a token, so it is never printed.
push_url="$(git -C "$workspace" remote get-url origin 2>/dev/null)" || exit 0
[[ -n "$push_url" ]] || exit 0

store="$(mktemp -d "${RUNNER_TEMP:-/tmp}/factory-vm-diag.XXXXXX")"
export GIT_DIR="$store/repo.git"
git init --quiet --bare "$GIT_DIR" || exit 0
export GIT_AUTHOR_NAME='factory-vm-sampler' GIT_AUTHOR_EMAIL='factory-vm-sampler@users.noreply.github.com'
export GIT_COMMITTER_NAME="$GIT_AUTHOR_NAME" GIT_COMMITTER_EMAIL="$GIT_AUTHOR_EMAIL"

stop_requested=0
trap 'stop_requested=1' TERM INT USR1

snapshot() {
  echo "utc: $(date -u +%FT%TZ)"
  echo "uptime: $(cut -d' ' -f1 /proc/uptime)s load: $(cut -d' ' -f1-3 /proc/loadavg)"
  echo "processes: $(ps -e --no-headers | wc -l)"
  echo '--- memory (MiB)'
  free -m
  echo '--- disk'
  df -h / "${RUNNER_TEMP:-/tmp}" 2>/dev/null
  echo '--- top rss (KiB, name only)'
  ps -eo rss=,comm= --sort=-rss | head -15
  echo '--- top cpu (name only)'
  ps -eo pcpu=,comm= --sort=-pcpu | head -8
  echo '--- kernel oom/kill'
  sudo -n dmesg 2>/dev/null | grep -iE 'out of memory|oom|killed process' | tail -5
  echo '--- shutdown/session signals'
  sudo -n journalctl -b --no-pager -o short-iso -n 400 2>/dev/null \
    | grep -iE 'oom|power ?off|reboot|shutdown|halt|systemd-logind|Stopping|killed' \
    | grep -viE 'token|secret|key' | tail -12
}

parent=''
publish() {
  local blob tree commit
  blob="$({ snapshot 2>&1 || true; } | git hash-object -w --stdin)" || return 1
  tree="$(printf '100644 blob %s\tsnapshot.txt\n' "$blob" | git mktree)" || return 1
  commit="$(git commit-tree "$tree" ${parent:+-p "$parent"} -m "vm snapshot run ${run_id}")" || return 1
  if GIT_TERMINAL_PROMPT=0 git push --quiet --force "$push_url" "$commit:$ref" >/dev/null 2>&1; then
    parent="$commit"
  fi
}

while (( stop_requested == 0 )); do
  publish || true
  sleep "$interval" &
  wait "$!" || true
done

# The session ended normally: this run needs no crash evidence.
GIT_TERMINAL_PROMPT=0 git push --quiet "$push_url" ":$ref" >/dev/null 2>&1 || true
rm -rf "$store"
