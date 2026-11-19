#!/usr/bin/env bash
set -euo pipefail

: "${VCR_IMAGE:?Set VCR_IMAGE to the VCR repository without a tag}"
: "${VERCEL_TOKEN:?Set VERCEL_TOKEN to a project-scoped Vercel access token}"
: "${VERCEL_TEAM_ID:?Set VERCEL_TEAM_ID to the Vercel team ID}"

ORAS_BIN="${ORAS_BIN:-oras}"
JQ_BIN="${JQ_BIN:-jq}"
GIT_BIN="${GIT_BIN:-git}"
AUTH_DIR="${AUTH_DIR:-$(mktemp -d)}"
REGISTRY_CONFIG="${REGISTRY_CONFIG:-$AUTH_DIR/oras-auth.json}"
KEEP_TAGS="${VCR_KEEP_TAGS:-}"

cleanup() {
  rm -rf "$AUTH_DIR"
}
trap cleanup EXIT

printf '%s' "$VERCEL_TOKEN" | "$ORAS_BIN" login \
  --registry-config "$REGISTRY_CONFIG" \
  --username "$VERCEL_TEAM_ID" \
  --password-stdin \
  vcr.vercel.com >/dev/null

mapfile -t tags < <(
  "$ORAS_BIN" repo tags \
    --registry-config "$REGISTRY_CONFIG" \
    --format json \
    "$VCR_IMAGE" | "$JQ_BIN" -r '.tags[]?'
)

if (( ${#tags[@]} == 0 )); then
  echo "VCR prune skipped: repository contains no tagged images."
  exit 0
fi

is_kept_tag() {
  local candidate="$1"
  local kept
  for kept in $KEEP_TAGS; do
    if [[ "$candidate" == "$kept" ]]; then
      return 0
    fi
  done
  return 1
}

created_epoch_for_tag() {
  local tag="$1"
  local created=""

  if [[ "$tag" =~ ^[0-9a-fA-F]{7,40}$ ]] && "$GIT_BIN" cat-file -e "${tag}^{commit}" 2>/dev/null; then
    "$GIT_BIN" show -s --format=%ct "$tag"
    return 0
  fi

  created="$($ORAS_BIN manifest fetch-config \
    --registry-config "$REGISTRY_CONFIG" \
    "$VCR_IMAGE:$tag" 2>/dev/null | "$JQ_BIN" -r '.created // empty' || true)"

  if [[ -n "$created" ]]; then
    date -u -d "$created" +%s 2>/dev/null || true
  fi
}

declare -A digest_for_tag=()
declare -A unique_digests=()
for tag in "${tags[@]}"; do
  digest="$($ORAS_BIN resolve --registry-config "$REGISTRY_CONFIG" "$VCR_IMAGE:$tag")"
  digest_for_tag["$tag"]="$digest"
  unique_digests["$digest"]=1
done

if (( ${#unique_digests[@]} <= 1 )); then
  echo "VCR prune skipped: ${#unique_digests[@]} unique image manifest(s) found; keeping the only deployable image."
  exit 0
fi

declare -A protected_digests=()
for tag in "${tags[@]}"; do
  if is_kept_tag "$tag"; then
    digest="${digest_for_tag[$tag]}"
    protected_digests["$digest"]=1
    echo "Protecting VCR image $tag ($digest)"
  fi
done

oldest_tag=""
oldest_digest=""
oldest_epoch=""
declare -A examined_digests=()

for tag in "${tags[@]}"; do
  digest="${digest_for_tag[$tag]}"

  if is_kept_tag "$tag" || [[ -n "${protected_digests[$digest]:-}" ]]; then
    continue
  fi
  if [[ -n "${examined_digests[$digest]:-}" ]]; then
    continue
  fi

  epoch="$(created_epoch_for_tag "$tag")"
  if [[ -z "$epoch" || ! "$epoch" =~ ^[0-9]+$ ]]; then
    echo "Skipping $tag: creation time could not be established safely."
    continue
  fi
  examined_digests["$digest"]=1

  if [[ -z "$oldest_epoch" || "$epoch" -lt "$oldest_epoch" ]]; then
    oldest_tag="$tag"
    oldest_digest="$digest"
    oldest_epoch="$epoch"
  fi
done

if [[ -z "$oldest_digest" ]]; then
  echo "VCR prune failed safely: no deletable image had a trustworthy creation time." >&2
  exit 1
fi

echo "Deleting oldest VCR image: $oldest_tag ($oldest_digest), created $(date -u -d "@$oldest_epoch" --iso-8601=seconds)"
"$ORAS_BIN" manifest delete \
  --registry-config "$REGISTRY_CONFIG" \
  --force \
  "$VCR_IMAGE@$oldest_digest"
