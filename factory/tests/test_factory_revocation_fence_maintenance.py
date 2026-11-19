"""The worker provenance fence must never close a noncanonical maintainer PR."""
from pathlib import Path

def test_noncanonical_branch_exemption_precedes_comment_api() -> None:
    workflow = Path(".github/workflows/factory-revocation-fence.yml").read_text()
    bypass = 'if [[ ! "$HEAD_REF" =~ ^factory/[0-9]+-[0-9]+- ]]; then'
    assert bypass in workflow
    assert workflow.index(bypass) < workflow.index("for attempt in {1..12}")
