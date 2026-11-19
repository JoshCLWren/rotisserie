"""Capture one GitHub Actions installation rate-limit observation.

Use only the job's GITHUB_TOKEN. Never fall back to gh's personal login.
The output is a private artifact, not a public Pages payload.
"""
import json
import os
from datetime import datetime, timezone
from urllib.request import Request, urlopen

BUCKETS = ("core", "graphql", "search")

def snapshot(payload, observed_at):
    resources = payload.get("resources", {})
    result = {"observed_at": observed_at, "credential_kind": "github-actions-installation",
              "public_alias": "factory-comicpile-actions", "resources": {}}
    for bucket in BUCKETS:
        raw = resources.get(bucket)
        if not isinstance(raw, dict):
            continue
        values = {key: raw.get(key) for key in ("limit", "remaining", "used", "reset")}
        if any(type(v) is not int or v < 0 for v in values.values()):
            continue
        if values["limit"] <= 0 or values["remaining"] > values["limit"] or values["used"] > values["limit"]:
            continue
        result["resources"][bucket] = {
            "limit": values["limit"], "remaining": values["remaining"], "used": values["used"],
            "reset_at": datetime.fromtimestamp(values["reset"], timezone.utc).isoformat().replace("+00:00", "Z"),
        }
    return result

def main():
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        raise SystemExit("GITHUB_TOKEN is required; do not use personal gh credentials")
    request = Request("https://api.github.com/rate_limit", headers={
        "Authorization": "Bearer " + token,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "comicpile-factory-quota-observer",
    })
    with urlopen(request, timeout=12) as response:
        payload = json.load(response)
    observed = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    output = snapshot(payload, observed)
    with open("factory-quota-observation.json", "w", encoding="utf-8") as stream:
        json.dump(output, stream, indent=2)
        stream.write("\n")
    print("Recorded factory Actions installation quota observation (private artifact).")

if __name__ == "__main__":
    main()
