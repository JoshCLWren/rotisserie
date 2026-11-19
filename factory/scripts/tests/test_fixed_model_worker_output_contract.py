"""Static contracts for fixed-model worker machine-readable outputs."""

from __future__ import annotations

import re
from pathlib import Path


WORKER = Path('.github/scripts/free-model-factory-worker.sh')


def _function_body(name: str) -> str:
    """Extract the function body from the worker script.

    Args:
        name: The name of the function to extract from the worker script.

    Returns:
        The function body text as a string.
    """
    text = WORKER.read_text(encoding='utf-8')
    match = re.search(rf'(?ms)^{re.escape(name)}\(\) \{{\n(.*?)^\}}$', text)
    assert match is not None, f'{name} function not found'
    return match.group(1)


def test_persist_issue_pr_stdout_is_reserved_for_pr_number() -> None:
    """Command substitution must capture only the numeric PR identifier."""
    body = _function_body('persist_issue_pr')

    assert 'git commit -m "factory: advance #${number} with ${DISPLAY}" >&2' in body
    assert 'git push --set-upstream origin "$branch" >&2' in body
    assert re.search(r'(?m)^  echo "\$pr"$', body)

    stdout_commands = [
        line.strip()
        for line in body.splitlines()
        if line.strip().startswith(('echo ', 'printf '))
        and not line.rstrip().endswith('>&2')
    ]
    assert stdout_commands == ['echo "$pr"']


def test_worker_config_environment_vars_remain_fail_fast() -> None:
    """Production worker identity must fail fast, never default silently.

    The worker must refuse to run when FACTORY_WORKER / FACTORY_SOURCE /
    FACTORY_MODEL / FACTORY_RUNTIME_MODEL are unset. A default like
    ``${FACTORY_WORKER:-test}`` would silently run an anonymous worker and is
    prohibited. The bash regression suite supplies its own test environment
    and must never require weakening this fail-fast property.
    """
    text = WORKER.read_text(encoding='utf-8')

    assert 'WORKER="${FACTORY_WORKER:?FACTORY_WORKER is required}"' in text
    assert 'SOURCE="${FACTORY_SOURCE:?FACTORY_SOURCE is required}"' in text
    assert 'MODEL="${FACTORY_MODEL:?FACTORY_MODEL is required}"' in text
    assert 'RUNTIME_MODEL="${FACTORY_RUNTIME_MODEL:?FACTORY_RUNTIME_MODEL is required}"' in text

    for var in ('FACTORY_WORKER', 'FACTORY_SOURCE', 'FACTORY_MODEL', 'FACTORY_RUNTIME_MODEL'):
        assert re.search(rf'{re.escape(var)}:-\$', text) is None, (
            f'{var} must not be given a silent default'
        )


def test_trusted_guard_is_staged_before_checkout() -> None:
    """The guard must be copied from the main checkout before any branch switch.

    This is the root-cause fix: an adopted PR branch carrying an older copy of
    fixed-model-guard.py must never be the implementation the worker executes.
    """
    body = _function_body('stage_trusted_guard')

    assert re.search(r'TRUSTED_GUARD="\$\{TRUSTED_GUARD:-\}"', body)
    assert re.search(r'TRUSTED_GUARD="\$\{TRUSTED_GUARD:-\}"\n', body)
    assert 'cp .github/scripts/fixed-model-guard.py "$TRUSTED_GUARD"' in body
    assert 'python3 "$TRUSTED_GUARD" --self-test' in body

    script = WORKER.read_text(encoding='utf-8')
    guard_index = script.index('stage_trusted_guard')
    loop_index = script.index('while ((')
    assert guard_index < loop_index


def test_stage_trusted_guard_keeps_tmp_fallback_in_production() -> None:
    """Production keeps its /tmp mktemp fallback; tests set TRUSTED_GUARD.

    The regression suite points TRUSTED_GUARD inside its own test workspace so
    the CI review agent never needs external-directory permission. Production
    must not be narrowed to satisfy the test: when TRUSTED_GUARD is unset the
    worker still stages the guard under /tmp via mktemp.
    """
    body = _function_body('stage_trusted_guard')

    assert re.search(r'TRUSTED_GUARD="\$\{TRUSTED_GUARD:-\}"', body)
    assert re.search(r'mktemp /tmp/comic-pile-fixed-model-guard\.XXXXXX\.py', body)
    assert 'cp .github/scripts/fixed-model-guard.py "$TRUSTED_GUARD"' in body


def test_reject_out_of_scope_diff_uses_trusted_guard_and_git_state() -> None:
    """The pre-push decision must be made by the trusted guard with git state.

    The worker must pass deterministic git-state flags (MERGE_HEAD, unmerged
    index entries, conflict markers, HEAD movement) to the trusted guard so a
    model-created merge/conflict is rejected before commit or push.
    """
    body = _function_body('reject_out_of_scope_diff')

    assert 'python3 "$TRUSTED_GUARD"' in body
    assert '.github/scripts/fixed-model-guard.py' not in body
    assert '--git-state "$git_state"' in body
    assert 'git_state="$(unclean_git_state_json)' in body

    for marker in ('MERGE_HEAD', 'CHERRY_PICK_HEAD', 'REVERT_HEAD', 'git ls-files -u', 'conflict'):
        assert marker in _function_body('unclean_git_state_json')


def test_persist_uses_expected_head_not_current_head() -> None:
    """Persistence must diff against and reset to the checked-out target head.

    Using `git rev-parse HEAD` would let a model-committed merge be treated as
    the base and then blindly persisted.
    """
    for fn in ('persist_issue_pr', 'persist_pr_changes'):
        body = _function_body(fn)
        assert 'base_ref="$EXPECTED_HEAD"' in body
        assert 'base_ref="$(git rev-parse HEAD)"' not in body


def test_checkout_target_records_expected_head() -> None:
    """checkout_target must record the branch head for fail-closed detection."""
    body = _function_body('checkout_target')
    assert 'EXPECTED_HEAD="$(git rev-parse HEAD)"' in body


def test_worker_rejects_orphaned_issue_lease_when_canonical_pr_exists() -> None:
    """A concurrent PR lease release cannot turn repair work into new issue work."""
    body = _function_body('select_controller_assignment')

    assert 'gh pr list --state open --limit 500 --json number,headRefName' in body
    assert 'test("^factory/[0-9]+-" + $issue + "-")' in body
    assert 'owns orphaned issue lease' in body
    assert 'while canonical open PR #${canonical_pr} exists' in body


def test_omniroute_and_nvidia_workers_reject_unclean_git_state() -> None:
    """Every factory wrapper must fail closed on model-created merge state."""
    for path in (Path('.github/scripts/omniroute-factory-worker.sh'), Path('.github/scripts/nvidia-factory-worker.sh')):
        script = path.read_text(encoding='utf-8')
        assert 'reject_unclean_git_state' in script
        assert 'MERGE_HEAD' in script
        assert 'CHERRY_PICK_HEAD' in script
        assert 'REVERT_HEAD' in script
        assert 'git ls-files -u' in script
        assert 'git reset --hard "$EXPECTED_HEAD"' in script
