"""Secret-safe operation records, metrics, and diagnostic bundles."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rotisserie.application import OperationResult

_SENSITIVE_KEY = re.compile(r"(authorization|credential|password|secret|token)", re.I)
_SENSITIVE_VALUE = re.compile(
    r"(?i)(bearer\s+[A-Za-z0-9._~+/=-]+|gh[opusr]_[A-Za-z0-9_]+|"
    r"github_pat_[A-Za-z0-9_]+|sk-[A-Za-z0-9_-]+|"
    r"(?:token|password|secret)\s*[:=]\s*\S+)"
)


def redact(value: Any, *, key: str = "") -> Any:
    """Recursively redact credential-shaped keys and values."""

    if _SENSITIVE_KEY.search(key):
        return "[REDACTED]"
    if isinstance(value, Mapping):
        return {str(k): redact(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    if isinstance(value, str):
        return _SENSITIVE_VALUE.sub("[REDACTED]", value)
    return value


@dataclass(frozen=True)
class OperationJournal:
    """Append-only JSONL records with derived metrics and diagnostics."""

    directory: Path

    @property
    def path(self) -> Path:
        return self.directory / "operations.jsonl"

    def append(
        self,
        *,
        correlation_id: str,
        command: str,
        dry_run: bool,
        result: OperationResult | Mapping[str, Any],
        timestamp: int,
    ) -> dict[str, Any]:
        result_value = result.to_dict() if isinstance(result, OperationResult) else dict(result)
        record = redact(
            {
                "schema_version": 1,
                "timestamp": timestamp,
                "correlation_id": correlation_id,
                "command": command,
                "dry_run": dry_run,
                "result": result_value,
            }
        )
        self.directory.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
        descriptor = os.open(self.path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            os.write(descriptor, encoded.encode())
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        assert isinstance(record, dict)
        return record

    def records(self) -> tuple[dict[str, Any], ...]:
        if not self.path.exists():
            return ()
        records: list[dict[str, Any]] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            value = json.loads(line)
            if not isinstance(value, dict) or value.get("schema_version") != 1:
                raise ValueError("unsupported operation record")
            records.append(value)
        return tuple(records)

    def metrics(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for record in self.records():
            result = record.get("result", {})
            status = result.get("status", "unknown") if isinstance(result, dict) else "unknown"
            key = f"operations.{record.get('command', 'unknown')}.{status}"
            counts[key] = counts.get(key, 0) + 1
        return {"schema_version": 1, "counters": dict(sorted(counts.items()))}

    def diagnostic_bundle(self, *, config: Mapping[str, Any], graph: Mapping[str, Any]) -> Path:
        bundle = redact(
            {
                "schema_version": 1,
                "config": dict(config),
                "graph": dict(graph),
                "metrics": self.metrics(),
                "recent_operations": self.records()[-20:],
            }
        )
        self.directory.mkdir(parents=True, exist_ok=True)
        target = self.directory / "diagnostics.json"
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(bundle, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        temporary.replace(target)
        return target
