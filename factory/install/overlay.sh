#!/usr/bin/env bash
# Copy the checked-out Rotisserie factory runtime into an adopter checkout.
#
# Usage: overlay.sh [adopter-dir]
#
# The copied paths are untracked and listed in the adopter's .git/info/exclude,
# so `git add -A` in a worker never commits them. Adopter state and config
# (.github/factory-*.json, .rotisserie/config.toml) stay tracked in the adopter.
set -Eeuo pipefail

factory_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
adopter="$(cd "${1:-.}" && pwd)"
paths_file="$factory_root/install/overlay-paths.txt"

[[ -d "$adopter/.git" ]] || { echo "overlay: $adopter is not a git checkout" >&2; exit 1; }
[[ -f "$paths_file" ]] || { echo "overlay: missing $paths_file" >&2; exit 1; }

exclude_file="$(git -C "$adopter" rev-parse --git-path info/exclude)"
[[ "$exclude_file" = /* ]] || exclude_file="$adopter/$exclude_file"
mkdir -p "$(dirname "$exclude_file")"
marker='# rotisserie factory overlay'
grep -qxF "$marker" "$exclude_file" 2>/dev/null || printf '\n%s\n' "$marker" >> "$exclude_file"
grep -qxF '/.rotisserie/runtime/' "$exclude_file" || printf '%s\n' '/.rotisserie/runtime/' >> "$exclude_file"

conflicts=()
while IFS= read -r path; do
  [[ -n "$path" && "$path" != \#* ]] || continue
  relative="${path%/}"
  if git -C "$adopter" ls-files --error-unmatch -- "$relative" >/dev/null 2>&1; then
    conflicts+=("$relative")
    continue
  fi
  mkdir -p "$adopter/$(dirname "$relative")"
  rm -rf "${adopter:?}/$relative"
  cp -R "$factory_root/$relative" "$adopter/$relative"
  grep -qxF "/$relative" "$exclude_file" || printf '/%s\n' "$relative" >> "$exclude_file"
done < "$paths_file"

if ((${#conflicts[@]})); then
  # A tracked copy means the adopter still carries the pre-cutover factory.
  # Overwriting it would put Rotisserie code into the adopter's diff.
  printf 'overlay: adopter still tracks factory paths; remove them first:\n' >&2
  printf '  %s\n' "${conflicts[@]}" >&2
  exit 1
fi

if [[ -n "${GITHUB_ENV:-}" ]]; then
  {
    printf 'ROTISSERIE_FACTORY_ROOT=%s\n' "$factory_root"
    printf 'ROTISSERIE_CONFIG=%s\n' "$adopter/.rotisserie/config.toml"
  } >> "$GITHUB_ENV"
fi

if [[ -n "$(git -C "$adopter" status --porcelain --untracked-files=all -- .github scripts docs prompts .opencode .agents)" ]]; then
  echo "overlay: copied files are visible to git; check .git/info/exclude" >&2
  git -C "$adopter" status --porcelain --untracked-files=all -- .github scripts docs prompts .opencode .agents >&2
  exit 1
fi
echo "overlay: installed Rotisserie factory runtime from $factory_root into $adopter"
