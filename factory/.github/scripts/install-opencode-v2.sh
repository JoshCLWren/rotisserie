#!/usr/bin/env bash
# Resolve the latest OpenCode 2 from the official npm channel at execution time.
set -Eeuo pipefail
root="${RUNNER_TEMP:-/tmp}/comic-pile-opencode-v2"
# The npm dist-tag can advance before platform-specific binaries are published.
# A missing @opencode/cli-linux-x64 tarball used to stop every reviewer before
# execution. Try recent published v2 releases in order and require a real CLI.
versions="$(npm view @opencode/cli versions --json)"
mapfile -t candidates < <(jq -r '
  [.[] | select(test("^2\\.[0-9]+\\.[0-9]+$"))] | reverse | .[:8][]
' <<< "$versions")
if (( ${#candidates[@]} == 0 )); then
  echo 'No published OpenCode v2 release candidates could be resolved' >&2
  exit 1
fi
binary="$root/bin/opencode"
version=''
installed=''
for candidate in "${candidates[@]}"; do
  # RUNNER_TEMP owns this prefix; do not reuse a partially installed release.
  rm -rf "$root"
  if ! npm install --global --prefix "$root" "@opencode/cli@$candidate"; then
    echo "OpenCode $candidate could not install; trying previous v2" >&2
    continue
  fi
  if [[ ! -x "$binary" ]]; then
    echo "OpenCode $candidate installed without an executable; retrying" >&2
    continue
  fi
  if ! version="$("$binary" --version)"; then
    echo "OpenCode $candidate executable failed; retrying" >&2
    continue
  fi
  if [[ "$version" =~ (^|[[:space:]])v?2\. ]]; then
    installed=true
    break
  fi
  echo "OpenCode $candidate returned unsupported version '$version'; retrying" >&2
done
if [[ "$installed" != true ]]; then
  echo 'No working published OpenCode v2 CLI among the newest eight releases' >&2
  exit 1
fi
echo "$root/bin" >> "${GITHUB_PATH:?GITHUB_PATH is required}"
# CI is noninteractive: authorize routine coding tools while keeping
# commits/pushes under the trusted factory wrapper, not the model.
mkdir -p "$HOME/.config/opencode"
cat > "$HOME/.config/opencode/opencode.json" <<'JSON'
{
  "$schema": "https://opencode.ai/config.json",
  "providers": {
    "opencode": {
      "models": {
        "crouton-alpha-free": {
          "body": {"safety_identifier": "comic-pile-factory"}
        }
      }
    }
  },
  "permissions": [
    {"action": "read", "resource": "*", "effect": "allow"},
    {"action": "edit", "resource": "*", "effect": "allow"},
    {"action": "shell", "resource": "*", "effect": "allow"},
    {"action": "glob", "resource": "*", "effect": "allow"},
    {"action": "grep", "resource": "*", "effect": "allow"},
    {"action": "skill", "resource": "*", "effect": "allow"},
    {"action": "subagent", "resource": "*", "effect": "allow"}
  ],
  "policies": [
    {"action": "tool.use", "resource": "shell:git push *", "effect": "deny"},
    {"action": "tool.use", "resource": "shell:git commit *", "effect": "deny"},
    {"action": "tool.use", "resource": "shell:git reset --hard *", "effect": "deny"}
  ]
}
JSON
chmod 600 "$HOME/.config/opencode/opencode.json"
printf 'OpenCode %s installed from the current release\n' "$version"
