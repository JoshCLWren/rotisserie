#!/usr/bin/env bash
# Run the real fixed-model worker locally against one issue or PR.
#
#   factory/local/run-worker.sh --pr 5417
#   factory/local/run-worker.sh --issue 5612 --model qwen3-coder:30b --budget 1800
#
# GitHub reads go through a host proxy that saves them; writes never leave
# this machine (see writes.jsonl). git pushes land in a local bare origin.
# The model runs through the production OpenCode 2 install against Ollama.
set -Eeuo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
factory="$(cd "$here/.." && pwd)"
repo_slug=''
adopter=
kind='' number='' model='qwen3-coder:30b' worker='1207147100200' budget=1800
stage='' offline=0 port=8787 workdir=''
while (( $# )); do
  case "$1" in
    --pr) kind=pr number="$2"; shift 2 ;;
    --issue) kind=issue number="$2"; shift 2 ;;
    --model) model="$2"; shift 2 ;;
    --worker) worker="$2"; shift 2 ;;
    --budget) budget="$2"; shift 2 ;;
    --stage) stage="$2"; shift 2 ;;
    --offline) offline=1; shift ;;
    --workdir) workdir="$2"; shift 2 ;;
    --port) port="$2"; shift 2 ;;
    --repo) repo_slug="$2"; shift 2 ;;
    --adopter) adopter="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
[[ "$repo_slug" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ && -n "$kind" && "$number" =~ ^[1-9][0-9]*$ ]] \
  || { echo 'usage: run-worker.sh --repo OWNER/NAME (--pr N | --issue N) [--adopter LOCAL_CLONE]' >&2; exit 2; }
workdir="${workdir:-$HOME/.cache/rotisserie-local/${repo_slug#*/}/${kind}-${number}}"
mkdir -p "$workdir"
run_id="$(date +%s)"
log() { printf '[local-run] %s\n' "$*" >&2; }

curl -sf -m 5 http://127.0.0.1:11434/api/tags >/dev/null || { echo 'Ollama is not running on :11434' >&2; exit 1; }
docker image inspect rotisserie-local-worker >/dev/null 2>&1 \
  || docker build -q -f "$here/Dockerfile" -t rotisserie-local-worker "$factory" >/dev/null

origin="$workdir/origin.git"
if [[ ! -d "$origin" ]]; then
  log "creating local bare origin from ${adopter:-GitHub}"
  git clone -q --bare "${adopter:-https://github.com/${repo_slug}.git}" "$origin"
fi
remote="https://github.com/${repo_slug}.git"
git -C "$origin" fetch -q "$remote" "+refs/heads/main:refs/heads/main"

if [[ "$kind" == pr ]]; then
  pr_json="$(gh pr view "$number" -R "$repo_slug" --json headRefName,headRefOid,labels)"
  head_ref="$(jq -r .headRefName <<< "$pr_json")"
  head_sha="$(jq -r .headRefOid <<< "$pr_json")"
  git -C "$origin" fetch -q "$remote" "+refs/pull/${number}/head:refs/pull/${number}/head" \
    "+refs/heads/${head_ref}:refs/heads/${head_ref}"
  [[ -n "$stage" ]] || stage="$(jq -r '[.labels[].name | select(test("^factory:(building|review|changes-requested|ci|ready|blocked)$"))][0] // "factory:changes-requested"' <<< "$pr_json")"
  assignment="$(jq -nc --arg w "$worker" --argjson n "$number" --arg h "$head_sha" --arg s "$stage" \
    '{kind:"pr",number:$n,worker:$w,head:$h,stage:$s}')"
else
  stage="${stage:-factory:building}"
  assignment="$(jq -nc --arg w "$worker" --argjson n "$number" '{kind:"issue",number:$n,worker:$w}')"
fi
seed="$(jq -nc --arg n "$number" --arg w "factory:$worker" --arg s "$stage" '{($n): ["factory", $w, $s]}')"

proxy_args=(--workdir "$workdir" --port "$port" --seed "$seed" --run-id "$run_id" --worker "$worker" --repo "$repo_slug")
(( offline )) && proxy_args+=(--offline)
python3 "$here/gh_proxy.py" "${proxy_args[@]}" 2>>"$workdir/proxy.log" &
proxy_pid=$!
trap 'kill "$proxy_pid" 2>/dev/null || true' EXIT
for _ in $(seq 1 20); do curl -s -m 1 "http://127.0.0.1:$port" >/dev/null 2>&1 && break; sleep 0.2; done

: > "$workdir/writes.jsonl"
log "running worker $worker on $kind #$number with ollama $model (budget ${budget}s, run $run_id)"
set +e
docker run --rm \
  --add-host api.github.com:127.0.0.1 --add-host github.com:127.0.0.1 \
  --add-host uploads.github.com:127.0.0.1 --add-host codeload.github.com:127.0.0.1 \
  -v "$origin:/origin.git" -v "$factory:/rotisserie-factory:ro" -v "$here/gh-shim:/usr/local/bin/gh:ro" \
  -v "$workdir:/out" \
  -e LOCAL_GH_PROXY="http://host.docker.internal:$port" \
  -e FACTORY_ASSIGNMENT="$assignment" -e FACTORY_WORKER="$worker" \
  -e FACTORY_SOURCE=ollama-cloud -e FACTORY_MODEL="$model" -e FACTORY_RUNTIME_MODEL="ollama-cloud/$model" \
  -e FACTORY_DISPLAY="local ollama · $model" -e FACTORY_BRANCH_SUFFIX=local-ollama \
  -e FACTORY_BUDGET_SECONDS="$budget" -e GITHUB_RUN_ID="$run_id" -e GITHUB_RUN_NUMBER=1 \
  -e GITHUB_REPOSITORY="$repo_slug" -e GH_TOKEN=local-proxy-no-token -e OLLAMA_API_KEY=ollama \
  rotisserie-local-worker bash -c '
    set -Eeuo pipefail
    export RUNNER_TEMP=/tmp/runner GITHUB_PATH=/tmp/runner/path GITHUB_ENV=/tmp/runner/env
    export GITHUB_STEP_SUMMARY=/tmp/runner/summary GITHUB_OUTPUT=/tmp/runner/output GITHUB_WORKSPACE=/work
    mkdir -p "$RUNNER_TEMP" && touch "$GITHUB_PATH" "$GITHUB_ENV" "$GITHUB_STEP_SUMMARY" "$GITHUB_OUTPUT"
    git clone -q /origin.git /work && cd /work
    git config user.name "opencode-free-model-factory-${FACTORY_WORKER}[bot]"
    git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
    mkdir -p .rotisserie/runtime/factory
    tar -C /rotisserie-factory --exclude=.venv --exclude=__pycache__ --exclude=.pytest_cache --exclude=local -cf - . \
      | tar -C .rotisserie/runtime/factory -xf -
    bash .rotisserie/runtime/factory/install/overlay.sh . >/dev/null
    for f in factory-neon-model-exclusions.json factory-fleet-sources.json factory-fleet-catalogs.json; do
      echo "{}" > "$RUNNER_TEMP/$f"
    done
    registry="$(python3 .github/scripts/factory_provider_registry.py config --source ollama-cloud --model "$FACTORY_MODEL")"
    jq --arg url http://host.docker.internal:11434/v1 ".providers[\"ollama-cloud\"].settings.baseURL = \$url" <<< "$registry" > /tmp/provider.json
    jq -s ".[0] * .[1]" /root/.config/opencode/opencode.json /tmp/provider.json > /tmp/oc.json
    mv /tmp/oc.json /root/.config/opencode/opencode.json
    set +e
    bash .github/scripts/free-model-factory-worker.sh 2>&1 | tee /out/worker.log
    status=${PIPESTATUS[0]}
    cp -f /tmp/opencode-factory-*.log /out/ 2>/dev/null
    git -C /work status --porcelain > /out/worktree-status.txt
    git -C /work diff > /out/worktree.diff
    exit "$status"
  '
status=$?
set -e
log "worker exit status $status"
log "GitHub writes it attempted (never sent): $(wc -l < "$workdir/writes.jsonl")  ->  $workdir/writes.jsonl"
[[ ! -s "$workdir/misses.txt" ]] || log "missing saved reads: $workdir/misses.txt"
log "pushes in local origin:"
git -C "$origin" for-each-ref --sort=-committerdate --count=5 --format='  %(refname:short) %(objectname:short) %(subject)' refs/heads/factory/ >&2 || true
log "worker log: $workdir/worker.log"
exit "$status"
