#!/usr/bin/env bash
# Exchange GitHub Actions workload identity for a short-lived OpenCode Console token.
# Source this function in the trusted runner, never from an adopted PR checkout.
factory_opencode_go_oidc() {
  # Disable xtrace before handling either token. Never print tokens to logs.
  set +x
  unset OPENCODE_API_KEY
  local client_id="${OPENCODE_GO_CLIENT_ID:-}"
  local audience="https://opencode.ai/console"
  [[ -n "$client_id" && -n "${ACTIONS_ID_TOKEN_REQUEST_URL:-}" && -n "${ACTIONS_ID_TOKEN_REQUEST_TOKEN:-}" ]] || return 1
  local encoded oidc_response oidc exchange_response access
  encoded="$(jq -rn --arg audience "$audience" '$audience | @uri')" || return 1
  oidc_response="$(curl -fsS --retry 2 --connect-timeout 5 --max-time 20 \
    --oauth2-bearer "$ACTIONS_ID_TOKEN_REQUEST_TOKEN" \
    "${ACTIONS_ID_TOKEN_REQUEST_URL}&audience=${encoded}")" || return 1
  oidc="$(jq -er '.value | select(type == "string" and length > 0)' <<< "$oidc_response")" || return 1
  exchange_response="$(curl -fsS --retry 2 --connect-timeout 5 --max-time 20 \
    https://opencode.ai/console/auth/oauth/token \
    --data-urlencode 'grant_type=urn:ietf:params:oauth:grant-type:token-exchange' \
    --data-urlencode 'subject_token_type=urn:ietf:params:oauth:token-type:jwt' \
    --data-urlencode "client_id=$client_id" \
    --data-urlencode "subject_token=$oidc")" || return 1
  access="$(jq -er '.access_token | select(type == "string" and length > 0)' <<< "$exchange_response")" || return 1
  # Register the mask before any use or environment-file persistence.
  printf '::add-mask::%s\n' "$access"
  export OPENCODE_API_KEY="$access"
  return 0
}
