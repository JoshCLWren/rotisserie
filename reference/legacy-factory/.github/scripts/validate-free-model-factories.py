#!/usr/bin/env python3
"""Validate the deterministic free-model factory roster and control plane."""

from __future__ import annotations

import importlib.util
import re
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
import os
import subprocess

MANIFEST = Path('.github/free-model-factories.tsv')
DISPATCHER = Path('.github/workflows/fixed-model-factory-dispatch.yml')
ENTRY = Path('.github/workflows/free-model-factory-entry.yml')
RUNNER = Path('.github/workflows/free-model-factory-run.yml')
DISCOVERY = Path('.github/workflows/chromium-discovery.yml')
MODEL_DISCOVERY = Path('.github/workflows/factory-model-discovery.yml')
DISCOVERY_CLASSIFIER = Path('.github/scripts/classify-chromium-discovery.py')
PLAYWRIGHT_CONFIG = Path('frontend/playwright.config.ts')
WORKER = Path('.github/scripts/free-model-factory-worker.sh')
PRIMITIVES = Path('.github/scripts/free-model-factory-worker-primitives.sh')
CONTROLLER = Path('.github/scripts/factory-work-controller.py')
POLICY = Path('.github/scripts/factory_work_policy.py')
KILO_HELPER = Path('.github/scripts/kilo-auto-factory-run.sh')
GUARD = Path('.github/scripts/fixed-model-guard.py')
ROSTER_HELPER = Path(__file__).resolve().parent / 'factory_roster.py'
ENTRY_PERMISSIONS = ('contents: write', 'issues: write', 'pull-requests: write', 'actions: write', 'checks: read')
OPENCODE_ALWAYS_FREE = frozenset({'big-pickle'})
OPENCODE_MUSE_SPARK_RE = re.compile(r'muse-spark', re.IGNORECASE)


def _load_factory_roster() -> ModuleType:
    """Load the shared roster helper without packaging ``.github``."""
    spec = importlib.util.spec_from_file_location('factory_roster_validate', ROSTER_HELPER)
    if spec is None or spec.loader is None:
        raise RuntimeError(f'unable to load factory roster helper: {ROSTER_HELPER}')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_ROSTER = _load_factory_roster()
SCHEDULE_MINUTES: tuple[int, ...] = _ROSTER.SCHEDULE_MINUTES
EXPECTED_WORKERS: set[int] = _ROSTER.expected_workers()
opencode_model_is_free: Callable[[str], bool] = _ROSTER.opencode_model_is_free
openrouter_model_is_free: Callable[[str], bool] = _ROSTER.openrouter_model_is_free
z_ai_model_is_free: Callable[[str], bool] = _ROSTER.z_ai_model_is_free
ollama_cloud_model_is_free: Callable[[str], bool] = _ROSTER.ollama_cloud_model_is_free
load_roster_rows = _ROSTER.load_roster_rows


def assert_free_provider_pins(rows: list[dict[str, str]]) -> None:
    """Fail closed when OpenCode/OpenRouter pins leave the free tier."""
    for row in rows:
        source = row['source']
        model = row['model']
        worker = row['worker']
        if source == 'opencode-free':
            assert opencode_model_is_free(model), (
                f'worker {worker} opencode-free pin must be a free OpenCode model '
                f'(big-pickle, *-free, or promo id), got {model!r}'
            )
        elif source == 'openrouter-free':
            assert openrouter_model_is_free(model), (
                f'worker {worker} openrouter-free pin must be an OpenRouter :free '
                f'model id, got {model!r}'
            )
        elif source == 'z-ai':
            assert z_ai_model_is_free(model), (
                f'worker {worker} z-ai pin must be glm-4.5-flash, got {model!r}'
            )
        elif source == 'ollama-cloud':
            assert ollama_cloud_model_is_free(model), (
                f'worker {worker} ollama-cloud pin must be a documented free '
                f'starter model, got {model!r}'
            )


def main() -> None:
    """Validate roster, runtime, lease, assignment, and discovery invariants."""
    rows = load_roster_rows(MANIFEST)

    assert len(rows) == len(EXPECTED_WORKERS), (
        f'expected {len(EXPECTED_WORKERS)} factory slots, got {len(rows)}'
    )
    workers = [int(row['worker']) for row in rows]
    assert set(workers) == EXPECTED_WORKERS
    assert len(workers) == len(set(workers)), 'duplicate worker IDs'
    assert_free_provider_pins(rows)

    kilo = [row for row in rows if row['source'] == 'kilo-auto']
    assert len(kilo) == 1 and kilo[0]['worker'] == '46'
    assert kilo[0]['model'] == 'kilo-auto/free'
    assert kilo[0]['display_name'] == 'Kilo Auto Free · Forge'

    counts: Counter[int] = Counter()
    for row in rows:
        minute = int(row['minute'])
        assert row['scheduler'] == 'dispatcher'
        assert minute in SCHEDULE_MINUTES
        counts[minute] += 1
    assert set(counts) == set(SCHEDULE_MINUTES)
    assert max(counts.values()) - min(counts.values()) <= 1
    assert sum(counts.values()) == len(EXPECTED_WORKERS)

    dispatcher = DISPATCHER.read_text(encoding='utf-8')
    assert 'workflow_run:' not in dispatcher
    assert 'schedule:' in dispatcher
    assert "cron: '7 * * * *'" in dispatcher
    assert dispatcher.count('    - cron:') == 1, 'dispatcher must use a single collapsed cron timer'
    assert 'offset="$(( ((GITHUB_RUN_NUMBER - 1) * offer_count) % total ))"' in dispatcher
    assert 'gh workflow run factory-ready-merge-drain.yml' in dispatcher
    assert 'elif [[ "$EVENT_NAME" == schedule || ( "$EVENT_NAME" == workflow_dispatch && "$DISPATCH_MODE" == roster ) ]]; then' in dispatcher
    assert "roster=\"$(awk -F '\\t' '" in dispatcher
    assert "!seen[$2]++ {print $1}" in dispatcher
    assert 'gh workflow run free-model-factory-entry.yml' in dispatcher
    assert "'.github/scripts/factory-work-controller.py'" in dispatcher
    assert "'.github/scripts/free-model-factory-worker.sh'" in dispatcher
    assert "'.github/scripts/kilo-auto-factory-run.sh'" in dispatcher
    for required in (
        'group: fixed-model-factory-dispatch',
        'queued in_progress',
        'gh run cancel "$run_id"',
        'release_cancelled_worker_leases',
        'factory:unowned',
        'another worker already owns it',
        'takeover observed',
        'python3 "$controller" reconcile',
        'python3 "$controller" capacity',
        'MAX_WORKERS_PER_TICK:-12',
        'source_seed=',
        'source-diverse seed',
        'OmniRoute free-entry cap is exhausted',
        'python3 "$controller" assign --worker "$worker"',
        'python3 "$controller" release --worker "$worker"',
        'while (( attempt <= 3 ))',
        'later workers were still attempted',
    ):
        assert required in dispatcher, f'deployment/assignment fence missing: {required}'

    entry = ENTRY.read_text(encoding='utf-8')
    assert 'workflow_dispatch:' in entry
    assert 'run-name: Factory ${{ inputs.worker }} · fixed-model entry' in entry
    assert 'uses: ./.github/workflows/free-model-factory-run.yml' in entry
    assert 'secrets: inherit' in entry
    for permission in ENTRY_PERMISSIONS:
        assert permission in entry

    runner = RUNNER.read_text(encoding='utf-8')
    assert 'group: fixed-model-factory-${{ inputs.worker }}' in runner
    assert 'cancel-in-progress: false' in runner
    # INCIDENT restore: multi-provider Entry is live; OmniRoute stays dark.
    assert 'NVIDIA_API_KEY: ${{ secrets.NVIDIA_API_KEY }}' in runner
    assert 'OPENROUTER_API_KEY: ${{ secrets.OPENROUTER_API_KEY }}' in runner
    assert 'Z_AI_API_KEY: ${{ secrets.Z_AI_API_KEY }}' in runner
    assert 'OLLAMA_API_KEY: ${{ secrets.OLLAMA_API_KEY }}' in runner
    assert 'nvidia)' in runner
    assert 'opencode-free|openrouter-free)' in runner
    assert 'kilo-auto)' in runner
    assert 'z-ai|ollama-cloud)' in runner
    assert 'nvidia|kilo-auto|z-ai|ollama-cloud)' in runner
    assert 'Configure OpenAI-compatible factory provider' in runner
    assert 'https://api.z.ai/api/paas/v4' in runner
    assert 'https://ollama.com/v1' in runner
    assert 'omniroute-disabled-incident' in runner
    assert 'FACTORY_OMNIROUTE_ENABLED' in runner
    # INCIDENT: catalog free-code lanes keep lane pins while OmniRoute is dark.
    assert 'selected-by-runtime-evidence' in runner
    assert 'starves restored free-code lanes' in runner
    assert 'if [[ "$model" == nvidia/* ]]' in runner or "if [[ \"$model\" == nvidia/* ]]" in runner
    assert "source='omniroute-free'" not in runner
    assert 'auto/best-free' not in runner
    assert 'FACTORY_OMNIROUTE_CAPACITY_BRIDGE' not in runner
    assert 'factory_omniroute_smoke.sh' not in runner
    assert 'Select execution candidate at dispatch time' in runner
    assert 'Probe pinned NVIDIA model before OpenCode smoke' in runner
    assert '"$code" == "000"' in runner
    assert '"$curl_exit" =~ ^(6|7|28|35|52|56)$' in runner
    assert 'provider_failure\\tNVIDIA probe HTTP' in runner
    assert 'Smoke exact pinned model through OpenCode' in runner
    assert "KILO_VERSION: '7.4.22'" in runner
    assert 'Smoke Kilo Auto Free through Kilo CLI' in runner
    assert 'PR_REBASE_TOKEN: ${{ secrets.PR_REBASE_TOKEN }}' in runner
    assert 'x-access-token:${PR_REBASE_TOKEN}' in runner

    assert MODEL_DISCOVERY.exists(), 'factory model discovery workflow is missing'
    model_discovery = MODEL_DISCOVERY.read_text(encoding='utf-8')
    for required in (
        'schedule:',
        'workflow_dispatch:',
        'factory_model_retirement.py',
        'opencode-model-catalog.sh',
        'OPENCODE_ZEN_API_KEY',
        'NVIDIA_API_KEY',
        'factory/model-retirement',
        'integrate.api.nvidia.com',
        '--retirement-comments',
        'issues/1093/comments',
        'factory-model-retired-410:v1',
        'add_unused_free',
        '--no-add-unused-free',
        '.github/free-model-factories.tsv',
        'unused free OpenCode models',
        'steps.plan.outputs.add',
    ):
        assert required in model_discovery, f'model discovery invariant missing: {required}'
    assert 'omniroute/auto' not in model_discovery
    assert 'auto/best-free' not in model_discovery
    discovery_version = re.search(r"OPENCODE_VERSION: '([^']+)'", model_discovery)
    runner_version = re.search(r"OPENCODE_VERSION: '([^']+)'", runner)
    discovery_sha = re.search(r"OPENCODE_LINUX_X64_SHA256: '([^']+)'", model_discovery)
    runner_sha = re.search(r"OPENCODE_LINUX_X64_SHA256: '([^']+)'", runner)
    assert discovery_version and runner_version, 'OpenCode version pin missing'
    assert discovery_sha and runner_sha, 'OpenCode sha256 pin missing'
    assert discovery_version.group(1) == runner_version.group(1), (
        'factory-model-discovery and free-model-factory-run must pin the same OpenCode version'
    )
    assert discovery_sha.group(1) == runner_sha.group(1), (
        'factory-model-discovery and free-model-factory-run must pin the same OpenCode sha256'
    )
    assert re.fullmatch(r'[0-9a-f]{64}', discovery_sha.group(1)), (
        'OpenCode sha256 pin must be a 64-char lowercase hex digest'
    )

    kilo_text = KILO_HELPER.read_text(encoding='utf-8')
    for required in (
        'unset KILO_API_KEY KILOCODE_API_KEY',
        'kilo run -m "$RUNTIME_MODEL" --auto --format json',
        'requested_route=kilo-auto/free',
        'step_finish',
        'non-zero cost',
    ):
        assert required in kilo_text, f'Kilo free-route invariant missing: {required}'

    guard = GUARD.read_text(encoding='utf-8')
    assert 'factory-control-out-of-scope' in guard and 'is_factory_control_path' in guard

    worker = WORKER.read_text(encoding='utf-8')
    assert 'OmniRoute Entry is disabled for this incident' in worker
    assert "omniroute-free" in worker
    assert 'OmniRoute-only' not in worker
    assert 'FACTORY_OMNIROUTE_ENABLED:-off' in worker
    assert 'trap' in worker and 'release_owned_targets omniroute-disabled-incident' in worker
    assert "auto/coding:free" not in runner
    assert "auto/reasoning:free" not in runner
    assert "FACTORY_OMNIROUTE_ENABLED: ${{ vars.FACTORY_OMNIROUTE_ENABLED || 'off' }}" in runner
    assert PRIMITIVES.exists(), 'tracked worker primitives are missing'
    primitives = PRIMITIVES.read_text(encoding='utf-8')
    assert "source <(sed '/^ensure_owner_label$/,$d' .github/scripts/free-model-factory-worker-primitives.sh)" in worker
    for required in (
        'release_owned_targets',
        'comic-pile-factory-implement-claim-v3',
        'comic-pile-factory-claim-released-v3',
        'issue_has_open_factory_pr',
        'reject_out_of_scope_diff',
        'fixed-model-guard.py',
        "[[ \"$SOURCE\" == 'kilo-auto' ]]",
        "[[ \"$SOURCE\" != 'kilo-auto' ]]",
        '.github/scripts/kilo-auto-factory-run.sh',
        'OmniRoute may switch upstream models, providers, or routes within the configured policy',
    ):
        assert required in primitives, f'inherited worker primitive missing: {required}'

    for required in (
        'select_controller_assignment',
        'no control-plane assignment is leased to this worker',
        'controller-assignment-read-failed',
        'session-end-handoff',
        'no-persisted-change-handoff',
        'stage_trusted_kilo_helper',
        'TRUSTED_KILO_HELPER',
        'handing it to the merge controller',
    ):
        assert required in worker, f'controller-assigned worker invariant missing: {required}'
    for forbidden in (
        'choose_existing_pr',
        'choose_ranked_issues',
        "claim_from_pool 'user-bug'",
        "claim_from_pool 'bug'",
        "claim_from_pool 'product'",
        'leased unowned PR',
        'gh pr merge "$NUMBER"',
        'trigger_backlog_zero_discovery',
    ):
        assert forbidden not in worker, f'worker still owns repo-wide selection/merge behavior: {forbidden}'
    assert 'gh workflow run chromium-discovery.yml' not in worker

    assert POLICY.exists(), 'tracked factory ranking policy module is missing'
    controller = CONTROLLER.read_text(encoding='utf-8')
    policy = POLICY.read_text(encoding='utf-8')
    for required in (
        'from factory_work_policy import',
        'def reconcile_stale_leases(',
        'def active_fixed_workers(',
        'def assign_candidate(',
        'def omniroute_free_entry_capacity(',
        'def in_flight_omniroute_free_entries(',
        'def release_worker(',
        'def inspect_assignment(',
        'comic-pile-factory-claim-released-v3',
        'omniroute_enabled',
        'FACTORY_OMNIROUTE_ENABLED',
        'DEFAULT_MULTI_PROVIDER_ENTRY_CAP',
        'latest_lease_activity_epoch',
        'queued',
        'in_progress',
    ):
        assert required in controller, f'factory controller runtime invariant missing: {required}'
    for required in (
        'class Candidate:',
        'def build_candidates(',
        "'e2e-discovered'",
        'return 4',
        'return 5',
        'factory:ready',
        'LOCAL_LEASE_TTL_SECONDS',
    ):
        assert required in policy, f'factory ranking/lease policy invariant missing: {required}'

    if DISCOVERY.exists():
        discovery = DISCOVERY.read_text(encoding='utf-8')
        for required in (
            'schedule:',
            "cron: '23 9 * * *'",
            'workflow_dispatch:',
            'fail-fast: false',
            'if: always()',
            'retention-days: 30',
            'playwright-report/',
            'test-results/',
            'discovery-artifacts/backend.log',
            'discovery-artifacts/run-metadata.json',
            'Classify persisted Chromium failures',
            'actions/download-artifact@v5',
            'classify-chromium-discovery.py',
            'issues: write',
        ):
            assert required in discovery, f'daily discovery invariant missing: {required}'
        assert 'cancel-in-progress: false' in discovery
        assert '\n  push:\n' not in discovery

    if PLAYWRIGHT_CONFIG.exists():
        playwright = PLAYWRIGHT_CONFIG.read_text(encoding='utf-8')
        for required in (
            "reporter: isCI ? [['github'], ['list']] : [['list']]",
            "trace: 'retain-on-failure'",
            "screenshot: 'only-on-failure'",
            "retries: isCI ? 2 : 0",
        ):
            assert required in playwright, f'Playwright failure-evidence invariant missing: {required}'

    if DISCOVERY_CLASSIFIER.exists():
        classifier = DISCOVERY_CLASSIFIER.read_text(encoding='utf-8')
        for required in (
            'chromium-discovery-failure:',
            'e2e-discovered',
            'e2e-infrastructure',
            'factory:unowned',
            'ralph-status:pending',
            'results.json',
            'GITHUB_RUN_ID',
            'GITHUB_SHA',
            'args = ["issue", "create"',
            'run_gh(*args)',
        ):
            assert required in classifier, f'discovery classifier invariant missing: {required}'
        assert re.search(
            r'run_gh\(\s*"issue",\s*"comment"',
            classifier,
        ), 'discovery classifier issue comment call missing'

    print(f'Validated {len(rows)} external factory lanes, centralized assignment, staggered scheduling, and daily Chromium discovery.')
    for minute in SCHEDULE_MINUTES:
        print(f'  :{minute:02d} -> {counts[minute]} workers')
    source_counts = Counter(row['source'] for row in rows)
    for source, count in sorted(source_counts.items()):
        print(f'  {source}: {count}')


    # Executable incident gate: omniroute-free must fail closed while dark.
    refuse = subprocess.run(
        ['bash', str(WORKER)],
        env={
            **os.environ,
            'FACTORY_WORKER': '99',
            'FACTORY_SOURCE': 'omniroute-free',
            'FACTORY_MODEL': 'auto/coding:free',
            'FACTORY_RUNTIME_MODEL': 'omniroute/auto/coding:free',
            'FACTORY_OMNIROUTE_ENABLED': 'off',
            'FACTORY_DISPLAY': 'incident-refuse',
            'FACTORY_BRANCH_SUFFIX': 'omniroute',
        },
        check=False,
        capture_output=True,
        text=True,
    )
    assert refuse.returncode != 0, refuse.stderr or refuse.stdout
    assert 'OmniRoute Entry is disabled for this incident' in (refuse.stderr + refuse.stdout)


if __name__ == '__main__':
    main()
