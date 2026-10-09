"""A credential-free trusted bridge demonstrating the version 1 process contract."""

import json
import sys

request = json.load(sys.stdin)
print(
    json.dumps(
        {
            "schema_version": 1,
            "assignment": request["assignment"]["id"],
            "attempt": request["attempt"],
            "result": {
                "outcome": "no_diff",
                "detail": "Example bridge received the assignment; no change was requested",
                "revision": None,
                "evidence": [],
                "resume": None,
                "retry_after_seconds": None,
            },
        }
    )
)
