"""Existing OpenCode factory worker uses the Go workspace, not Zen credits."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".github/scripts"))

from factory_opencode_go import GoCatalogError, catalog, choose, free_models  # noqa: E402
from factory_model_failover import ExhaustedCandidatesError, select_next, select_next_fleet  # noqa: E402
from factory_dispatch_credential_filter import source_requirements  # noqa: E402


def sample() -> dict:
    def model(inp=0, out=0, *, tools=True, kind="chat", cache=None):
        return {
            "type": kind,
            "capabilities": {"tools": {"supported": tools}},
            "cost": {"text": {"input": {"price": inp, "cache": cache or {}},
                              "output": {"price": out}}},
        }
    return {
        "opencode": {"models": {"zen-paid": model(4, 15)}},
        "opencode-go": {"models": {
            "hy4-flash-free": model(),
            "other-free": model(),
            "paid-go": model(1, 5),
            "cache-charged": model(cache={"write": 0.3}),
            "unpriced": {"type": "chat"},
            "no-tools": model(tools=False),
            "embed": model(kind="embedding"),
        }},
    }


def test_hy4_preferred_only_from_go_zero_cost_catalog():
    assert free_models(sample()) == ["hy4-flash-free", "other-free"]
    selected = choose(sample(), "41")
    assert selected["source"] == "opencode-free"  # Preserve durable leases.
    assert selected["runtime_model"] == "opencode-go/hy4-flash-free"
    assert selected["model"] == "hy4-flash-free"


def test_other_zero_cost_go_models_survive_exact_model_cooldown():
    picked = choose(sample(), "41", excluded={"hy4-flash-free"})
    assert picked["runtime_model"] == "opencode-go/other-free"
    with pytest.raises(GoCatalogError):
        choose(sample(), "41", excluded={"hy4-flash-free", "other-free"})
    picked = select_next("opencode-free", "41", set(),
                         {"opencode-free": ["hy4-flash-free"]}, catalog_json=sample())
    assert picked["model"] == "other-free"
    with pytest.raises(ExhaustedCandidatesError):
        select_next("opencode-free", "41", set(),
                    {"opencode-free": ["hy4-flash-free", "other-free"]},
                    catalog_json=sample())


def test_zen_key_is_not_go_key_and_worker_requires_named_go_secret():
    assert source_requirements("opencode-free") == ("FACTORY_HAS_OPENCODE_GO_API_KEY",)
    with pytest.raises(GoCatalogError, match="missing"):
        catalog({"OPENCODE_ZEN_API_KEY": "only-zen", "OPENCODE_GO_API_KEY": ""})
    workflow = (ROOT / ".github/workflows/free-model-factory-run.yml").read_text()
    assert "OPENCODE_API_KEY: ${{ secrets.OPENCODE_GO_API_KEY }}" in workflow
    assert "secrets.OPENCODE_ZEN_API_KEY" not in workflow
    assert "secrets.OPENCODE_API_KEY" not in workflow


def test_go_first_then_other_provider_if_all_free_go_cooling():
    pools = {"opencode-free": sample(), "nvidia": {"data": [{"id": "sample/backup"}]}}
    picked = select_next_fleet("41", "nvidia", set(), set(), {}, pools,
                               set(pools), proven=set())
    assert picked["runtime_model"] == "opencode-go/hy4-flash-free"
    picked = select_next_fleet("41", "nvidia", {("opencode-free", "hy4-flash-free")},
                               set(), {"opencode-free": ["other-free"]},
                               pools, set(pools), proven=set())
    assert picked["source"] == "nvidia"
    assert select_next_fleet("41", "nvidia", set(), set(), {}, pools,
                             {"nvidia"}, proven=set())["source"] == "nvidia"


def test_authenticated_catalog_rejects_key_without_go_provider(monkeypatch):
    class Response:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def read(self, _): return b'{"opencode":{"models":{}}}'

    monkeypatch.setattr("factory_opencode_go.urllib.request.urlopen",
                        lambda *_args, **_kwargs: Response())
    with pytest.raises(GoCatalogError, match="subscription"):
        catalog({"OPENCODE_API_KEY": "dummy-test-key"})
