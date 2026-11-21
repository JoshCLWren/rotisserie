"""The runtime learns its adopter from the environment or the adopter's config only."""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / ".github" / "scripts"))
import factory_adopter  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_config(monkeypatch: pytest.MonkeyPatch) -> None:
    factory_adopter.load_config.cache_clear()
    yield
    factory_adopter.load_config.cache_clear()


def write_config(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(text)
    return path


def test_environment_identity_wins(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("GITHUB_REPOSITORY", "acme/widgets")
    monkeypatch.setenv("ROTISSERIE_CONFIG", str(write_config(tmp_path, '[repository]\nowner = "x"\nname = "y"\n')))
    assert factory_adopter.repository() == "acme/widgets"


def test_config_supplies_identity_without_the_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    monkeypatch.setenv("ROTISSERIE_CONFIG", str(write_config(tmp_path, '[repository]\nowner = "acme"\nname = "gears"\n')))
    assert factory_adopter.repository() == "acme/gears"


@pytest.mark.parametrize("text", ["", "[repository]\nowner = \"acme\"\n", "[markers]\nnamespace = \"x\"\n"])
def test_unknown_identity_fails_loudly(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, text: str) -> None:
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    monkeypatch.setenv("ROTISSERIE_CONFIG", str(write_config(tmp_path, text)))
    with pytest.raises(factory_adopter.AdopterConfigError):
        factory_adopter.repository()


def test_invalid_identity_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_REPOSITORY", "not a slug")
    with pytest.raises(factory_adopter.AdopterConfigError):
        factory_adopter.repository()


def test_runtime_has_no_default_adopter() -> None:
    scripts = Path(__file__).resolve().parents[1] / ".github" / "scripts"
    for path in scripts.glob("*.py"):
        if path.name.startswith("test_"):
            continue
        text = path.read_text(encoding="utf-8")
        assert not re.search(r'GITHUB_REPOSITORY"\s*,\s*"[^"]', text), path.name
        assert 'setdefault("GITHUB_REPOSITORY"' not in text, path.name


def test_runtime_scripts_never_name_a_fixed_adopter_repository() -> None:
    import re
    from pathlib import Path

    scripts = Path(__file__).resolve().parents[1] / ".github" / "scripts"
    pattern = re.compile(r'name\s*[:=]\s*\\?"comic-pile\\?"|"name":\s*"comic-pile"')
    offenders = [
        path.name for path in scripts.glob("*.py")
        if not path.name.startswith("test_") and pattern.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []
