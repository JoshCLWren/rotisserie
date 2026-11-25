"""Clear one model's health bench and backoff rows, and nothing else.

The full reset (``reset_model_health``) and the quota-only reset clear every
model in the repository. This one deletes the ``factory_model_health`` rows
and the model-scoped ``factory_backoff`` rows of one model (optionally of one
source) and returns each deleted row, so the operator sees what was cleared.
Worker, provider and pool backoffs stay, and so does the repository's reset
marker: a later failure of this model can still count recent history.
"""

from __future__ import annotations

import re
from typing import Any

import factory_turso_core as core
from factory_turso_core import REPO_RE

MODEL_RE = re.compile(r"^[A-Za-z0-9_./:+-]{1,180}$")
SOURCE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")


def reset_one_model(repo: str, model: str, source: str = "") -> dict[str, Any]:
    if not REPO_RE.fullmatch(repo):
        raise ValueError("invalid repository")
    if not MODEL_RE.fullmatch(model):
        raise ValueError("invalid model")
    if source and not SOURCE_RE.fullmatch(source):
        raise ValueError("invalid source")
    params = {"repo": repo, "model": model, "source": source}
    scope = "repo = :repo AND model = :model AND (:source = '' OR source = :source)"
    health, backoffs = core.store().batch(
        [
            (
                f"DELETE FROM factory_model_health WHERE {scope}"
                " RETURNING source, model, status, last_outcome, last_run_id",
                params,
            ),
            (
                f"DELETE FROM factory_backoff WHERE kind = 'model' AND {scope}"
                " RETURNING source, model, outcome, streak, run_id",
                params,
            ),
        ]
    )
    left = core.store().read(
        f"SELECT count(*) AS n FROM factory_backoff WHERE kind = 'worker' AND {scope}", params
    )
    return {
        "model": model,
        "source": source or None,
        "model_health_rows": [dict(row) for row in health],
        "backoff_rows": [dict(row) for row in backoffs],
        "worker_backoffs_kept": int(left[0]["n"]) if left else 0,
    }
