"""Console tests serve the fake adopter acme/widgets.

The factory runtime modules derive their comment-marker prefix from the
adopter repository, so every test binds the same one.
"""

from __future__ import annotations

import pytest

from rotisserie.factory_console import runtime

FAKE_ADOPTER = "acme/widgets"
runtime.bind_repository(FAKE_ADOPTER)


@pytest.fixture(autouse=True)
def fake_adopter() -> None:
    runtime.bind_repository(FAKE_ADOPTER)


@pytest.fixture(autouse=True)
def private_read_cache(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No test may read or write the operator's real GitHub read cache."""
    monkeypatch.setenv("ROTISSERIE_CACHE_DIR", str(tmp_path_factory.mktemp("read-cache")))
