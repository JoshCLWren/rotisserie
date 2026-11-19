"""Adopter configuration: repository identity and bring-your-own providers."""

from __future__ import annotations

import importlib
import re
import sys
from pathlib import Path

import pytest

FACTORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(FACTORY / ".github" / "scripts"))
config = importlib.import_module("factory_config")

ENV = {"ROTISSERIE_FACTORY_ROOT": str(FACTORY)}


def _load(tmp_path: Path, text: str, **env: str) -> object:
    path = tmp_path / "config.toml"
    path.write_text(text)
    return config.load({**ENV, "ROTISSERIE_CONFIG": str(path), **env})


def test_readme_example_extends_disables_and_adds_providers(tmp_path: Path) -> None:
    readme = (FACTORY / "README.md").read_text()
    example = re.search(
        r"`\.rotisserie/config\.toml` in the adopter:\n\n```toml\n(.*?)```", readme, re.S
    )
    assert example
    loaded = _load(tmp_path, example.group(1))
    assert loaded.repository == "your-org/your-repo"
    assert "my-gateway" in loaded.providers
    assert "pollinations" not in loaded.providers
    assert "nvidia" in loaded.providers
    assert "MY_GATEWAY_API_KEY" in loaded.secret_names()


def test_replace_mode_uses_only_declared_providers(tmp_path: Path) -> None:
    loaded = _load(
        tmp_path,
        '[repository]\nowner = "o"\nname = "r"\n'
        '[providers]\nmode = "replace"\n'
        '[providers.local]\ncredential = "LOCAL_KEY"\nendpoint = "http://localhost:11434/v1"\n',
    )
    assert list(loaded.providers) == ["local"]
    assert loaded.providers["local"].provider == "local"
    assert loaded.secret_names() == ("LOCAL_KEY",)


def test_endpoint_and_catalog_placeholders_expand_from_extra_credentials(tmp_path: Path) -> None:
    loaded = _load(tmp_path, '[repository]\nowner = "o"\nname = "r"\n')
    spec = loaded.providers["cloudflare-workers-ai"]
    env = {"CLOUDFLARE_API_TOKEN": "t", "CLOUDFLARE_ACCOUNT_ID": "acct"}
    assert spec.resolved_endpoint(env) == "https://api.cloudflare.com/client/v4/accounts/acct/ai/v1"
    assert "/accounts/acct/ai/models/search" in spec.resolved_catalog_url(env)
    assert (
        loaded.providers["groq-free"].resolved_catalog_url({})
        == "https://api.groq.com/openai/v1/models"
    )


def test_actions_repository_wins_and_local_runs_read_the_file(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('[repository]\nowner = "file"\nname = "repo"\n')
    local = {**ENV, "ROTISSERIE_CONFIG": str(path)}
    assert config.repository(local) == "file/repo"
    assert config.repository({**local, "GITHUB_REPOSITORY": "actions/repo"}) == "actions/repo"


def test_unknown_repository_fails_only_when_asked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    loaded = config.load(dict(ENV))
    assert loaded.providers
    with pytest.raises(config.ConfigError):
        _ = loaded.repository


def test_missing_config_file_named_explicitly_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(config.ConfigError):
        config.load({**ENV, "ROTISSERIE_CONFIG": str(tmp_path / "missing.toml")})


def test_provider_without_credential_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(config.ConfigError, match="credential"):
        _load(tmp_path, '[providers.bad]\nendpoint = "https://x/v1"\n')


def test_missing_defaults_fail_closed(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('[repository]\nowner = "o"\nname = "r"\n')
    with pytest.raises(config.ConfigError, match="default providers"):
        config.load({"ROTISSERIE_FACTORY_ROOT": str(tmp_path), "ROTISSERIE_CONFIG": str(path)})
