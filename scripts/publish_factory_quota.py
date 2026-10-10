"""Produce an allowlisted public view of *observed* installation quotas.

Input is a trusted private collection artifact, never public raw headers/logs.
Unverified measurements are omitted instead of replaced with personal quotas.
"""
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

BUCKETS = ("core", "graphql", "search")
def iso(value):
    try:
        date = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if date.tzinfo is None:
            raise ValueError("timezone required")
        return date.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    except (TypeError, AttributeError, ValueError):
        return None

def integer(value):
    return value if type(value) is int and value >= 0 else None

def sanitize(raw):
    published = iso(raw.get("observed_at"))
    if not published:
        raise ValueError("trusted observation timestamp required")
    installations = []
    # Accept an authenticated ComicPile observer sample as well as an
    # aggregate built from multiple private observations.
    observations = raw.get("installations")
    if observations is None and raw.get("credential_kind") == "github-actions-installation":
        observations = [raw]
    for row in observations or []:
        # Use only internally assigned display aliases; never emit installation
        # IDs, repository metadata, headers, tokens, or raw consumer URLs.
        alias = row.get("public_alias")
        if not isinstance(alias, str) or not alias.startswith("factory-") or len(alias) > 30 or not all(c.islower() or c.isdigit() or c == "-" for c in alias):
            continue
        clean = {"alias": alias}
        for kind in BUCKETS:
            value = row.get("resources", row).get(kind, {})
            if not isinstance(value, dict):
                continue
            limit, remaining, used = (integer(value.get(key)) for key in ("limit", "remaining", "used"))
            reset = iso(value.get("reset_at"))
            if any(v is None for v in (limit, remaining, used)) or limit <= 0 or remaining > limit or used > limit or not reset:
                continue
            clean[kind] = {"limit": limit, "remaining": remaining, "used": used, "reset_at": reset}
            low = integer(value.get("low_water_24h"))
            if low is not None and low <= limit:
                clean[kind]["low_water_24h"] = low
        installations.append(clean)
    return {
        "schema_version": 1,
        "published_at": published,
        "installations": installations,
        "exhaustion_incidents_24h": integer(raw.get("exhaustion_incidents_24h")),
        # No names of workflow paths, endpoint URLs, or private repositories.
        "top_consumers": [],
        "actions": {"summary": "Unavailable"},
        "ai_models": {"summary": "Unavailable"},
    }

def main():
    if len(sys.argv) != 3:
        raise SystemExit("usage: publish_factory_quota.py private-observations.json public-data.json")
    raw = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    output = sanitize(raw)
    Path(sys.argv[2]).parent.mkdir(parents=True, exist_ok=True)
    Path(sys.argv[2]).write_text(json.dumps(output, indent=2, sort_keys=True) + "\n", encoding="utf-8")

if __name__ == "__main__":
    main()
