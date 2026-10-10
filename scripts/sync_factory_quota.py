"""Publish a strictly sanitized 24-hour API budget snapshot from private Actions artifacts.

The publisher runs only in Rotisserie's trusted main-branch workflow.
It never exports the downloaded artifacts or any repository metadata.
"""
from __future__ import annotations

import base64
import io
import json
import os
import subprocess
import sys
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from scripts.publish_factory_quota import sanitize

SOURCE = "JoshCLWren/comic-pile"
DESTINATION = "JoshCLWren/JoshCLWren.github.io"
ARTIFACT_PREFIX = "factory-api-quota-"
MAX_ARTIFACTS = 26
MAX_OBSERVATION_BYTES = 32768


def utc(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("timezone required")
    return dt.astimezone(UTC)


def api(path: str, *args: str) -> bytes:
    result = subprocess.run(
        ["gh", "api", path, *args],
        check=True,
        capture_output=True,
        env=os.environ.copy(),
    )
    return result.stdout


def observations_from_artifacts(now: datetime) -> list[dict[str, Any]]:
    response = json.loads(api(f"repos/{SOURCE}/actions/artifacts?per_page=100"))
    candidates = [
        item for item in response.get("artifacts", [])
        if isinstance(item.get("name"), str)
        and item["name"].startswith(ARTIFACT_PREFIX)
        and not item.get("expired")
        and utc(item["created_at"]) >= now - timedelta(hours=25)
    ]
    candidates.sort(key=lambda item: item["created_at"], reverse=True)
    observations: list[dict[str, Any]] = []
    for item in candidates[:MAX_ARTIFACTS]:
        archive = api(f"repos/{SOURCE}/actions/artifacts/{int(item['id'])}/zip")
        with zipfile.ZipFile(io.BytesIO(archive)) as z:
            names = z.namelist()
            if names != ["factory-quota-observation.json"]:
                raise ValueError("unexpected artifact layout")
            if z.getinfo(names[0]).file_size > MAX_OBSERVATION_BYTES:
                raise ValueError("oversized quota observation")
            sample = json.loads(z.read(names[0]))
        if sample.get("credential_kind") != "github-actions-installation":
            continue
        timestamp = utc(sample["observed_at"])
        if not now - timedelta(hours=24) <= timestamp <= now + timedelta(minutes=5):
            continue
        # Refuse anything other than explicitly identified observation sources.
        if sample.get("public_alias") != "factory-comicpile-actions":
            continue
        observations.append(sample)
    return observations


def aggregate(samples: list[dict[str, Any]], now: datetime) -> dict[str, Any]:
    if not samples:
        raise ValueError("No authenticated, recent installation observations; refusing publication")
    samples = sorted(samples, key=lambda s: utc(s["observed_at"]))
    latest = samples[-1]
    raw: dict[str, Any] = {
        "observed_at": now.isoformat(),
        "installations": [latest],
    }
    result = sanitize(raw)
    if not result["installations"] or not any(
        bucket in result["installations"][0] for bucket in ("core", "graphql", "search")
    ):
        raise ValueError("No validated quota buckets; refusing publication")
    current = result["installations"][0]
    current["observed_at"] = utc(latest["observed_at"]).isoformat()
    for bucket in ("core", "graphql", "search"):
        if bucket not in current:
            continue
        values = [
            s.get("resources", {}).get(bucket, {}).get("remaining")
            for s in samples
        ]
        valid = [v for v in values if type(v) is int and 0 <= v <= current[bucket]["limit"]]
        if valid:
            current[bucket]["low_water_24h"] = min(valid)
    # Count observed transitions into exhaustion, not total zero-valued samples.
    exhausted = False
    incidents = 0
    for sample in samples:
        remaining = sample.get("resources", {}).get("core", {}).get("remaining")
        if type(remaining) is not int:
            continue
        is_exhausted = remaining == 0
        if is_exhausted and not exhausted:
            incidents += 1
        exhausted = is_exhausted
    result["exhaustion_incidents_24h"] = incidents
    # Endpoint attribution and other credential pools are not measured yet.
    result["top_consumers"] = None
    return result


def main() -> None:
    if not os.environ.get("GH_TOKEN"):
        raise SystemExit("Missing dedicated GitHub App publishing token")
    now = datetime.now(UTC)
    payload = aggregate(observations_from_artifacts(now), now)
    path = f"repos/{DESTINATION}/contents/factory-health/data.json"
    encoded = base64.b64encode((json.dumps(payload, sort_keys=True, indent=2) + "\n").encode()).decode()
    try:
        existing = json.loads(api(path))
        sha = existing["sha"]
    except subprocess.CalledProcessError as error:
        # A nonexistent file is normal only on initial publication.
        if b"404" not in error.stderr:
            raise
        sha = None
    fields = [
        "--method", "PUT", "-f", "branch=main",
        "-f", "message=chore: refresh sanitized factory API budgets",
        "-f", f"content={encoded}",
    ]
    if sha:
        fields += ["-f", f"sha={sha}"]
    api(path, *fields)
    print("Published sanitized quota observations; no private source data emitted.")


if __name__ == "__main__":
    main()
