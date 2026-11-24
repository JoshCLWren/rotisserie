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
