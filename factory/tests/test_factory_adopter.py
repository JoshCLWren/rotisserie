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


def test_worker_exports_are_empty_without_neon_unless_required(monkeypatch) -> None:
    import importlib
    import sys
    from pathlib import Path

    import pytest

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / ".github" / "scripts"))
    ops = importlib.import_module("factory_state_operations")
    monkeypatch.setattr(ops, "enabled", lambda: False)
    monkeypatch.delenv("FACTORY_NEON_LEASE_REQUIRED", raising=False)
    assert ops.export_model_exclusions("o/r", "1") == {}
    assert ops.export_candidate_health("o/r") == {"models": []}
    assert ops.export_fleet_failover_sources("o/r", "1") == []
    monkeypatch.setenv("FACTORY_NEON_LEASE_REQUIRED", "true")
    with pytest.raises(RuntimeError):
        ops.export_model_exclusions("o/r", "1")


def test_adopter_fast_check_command_replaces_builtin_checks(tmp_path, monkeypatch) -> None:
    import os
    import subprocess
    import sys
    from pathlib import Path

    script = Path(__file__).resolve().parents[1] / ".github" / "scripts" / "factory-fast-checks.sh"
    config = tmp_path / ".rotisserie" / "config.toml"
    config.parent.mkdir()
    for fast, expected in (('exit 0', 0), ('echo boom; exit 3', 3)):
        config.write_text(f'[checks]\nfast = "{fast}"\n')
        result = subprocess.run(
            ["bash", "-c", f'source "{script}"; factory_run_fast_checks HEAD "{tmp_path}/log"'],
            cwd=tmp_path, capture_output=True, text=True,
            env={**os.environ, "ROTISSERIE_CONFIG": str(config), "PATH": f"{Path(sys.executable).parent}:{os.environ['PATH']}"},
        )
        assert result.returncode == expected
        assert "adopter fast checks" in (tmp_path / "log").read_text()


def test_acceptance_report_identity_and_pass_synonyms_are_normalized() -> None:
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / ".github" / "scripts" / "factory_issue_acceptance.py"
    spec = importlib.util.spec_from_file_location("factory_issue_acceptance_norm", path)
    acc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(acc)
    head, digest = "a" * 40, "b" * 64
    mission = f"ISSUE ACCEPTANCE: #7, head {head}, issue digest {digest}.\nACCEPTANCE_CRITERIA_COUNT=2\n"
    report = {"issue": 7, "head": head, "issue_digest": "c" * 64,
              "items": [{"id": "A1", "status": "Verified"}, {"id": "A2", "status": "blocked"}]}
    fixed = acc.normalize_report(report, mission)
    assert fixed["issue_digest"] == digest
    assert [item["status"] for item in fixed["items"]] == ["pass", "blocked"]
    assert acc.normalize_report(None, mission) is None
    text = "review\n" + acc.PREFIX + "{}\nFACTORY_GATE_READY"
    assert acc.report_from_text(acc.replace_report_line(text, fixed)) == fixed


def test_reserved_issues_default_to_none(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ROTISSERIE_CONFIG", str(write_config(tmp_path, '[repository]\nowner = "a"\nname = "b"\n')))
    assert factory_adopter.reserved_issues() == frozenset()


def test_reserved_issues_come_from_the_adopter_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ROTISSERIE_CONFIG", str(write_config(tmp_path, "[issues]\nreserved = [679, 1093]\n")))
    assert factory_adopter.reserved_issues() == frozenset({679, 1093})


@pytest.mark.parametrize("value", ['"679"', "[0]", '["679"]'])
def test_invalid_reserved_issues_fail_loudly(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, value: str) -> None:
    monkeypatch.setenv("ROTISSERIE_CONFIG", str(write_config(tmp_path, f"[issues]\nreserved = {value}\n")))
    with pytest.raises(factory_adopter.AdopterConfigError):
        factory_adopter.reserved_issues()
