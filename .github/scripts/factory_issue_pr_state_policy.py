#!/usr/bin/env python3
"""Pure issue-state precedence for trusted canonical factory PR history."""

from __future__ import annotations

import argparse
import json
from collections.abc import Iterable, Mapping
from typing import Any


def stage_from_trusted_prs(records: Iterable[Mapping[str, Any]]) -> str:
    """Derive issue lifecycle from trusted canonical PR history.

    Durable merged provenance permanently wins. Without a merged PR, any open
    trusted canonical PR owns the lifecycle. Only when every trusted attempt is
    closed unmerged does the issue return to claimable backlog state.
    """
    trusted = [record for record in records if bool(record.get("trusted"))]
    if any(bool(record.get("merged")) for record in trusted):
        return "factory:ready"
    if any(str(record.get("state") or "").upper() == "OPEN" for record in trusted):
        return "factory:review"
    return "claimable"


def main() -> int:
    """Parse trusted PR records and print the derived issue lifecycle stage."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--records-json", required=True)
    args = parser.parse_args()
    records = json.loads(args.records_json)
    if not isinstance(records, list):
        parser.error("--records-json must decode to a list")
    print(stage_from_trusted_prs(records))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
