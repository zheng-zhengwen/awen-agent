"""Characterize the public project identity before and after a rename."""
from __future__ import annotations

import importlib
import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LEGACY_STEM = "ivy" + "ea"


def test_project_identity_is_consistent(monkeypatch, tmp_path):
    """Package, CLI entry point, and state root use one canonical identity."""
    monkeypatch.setenv("AWEN_HOME", str(tmp_path))

    from awen_agent import config

    importlib.reload(config)
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert config.AWEN_DIR == tmp_path
    assert 'name = "awen-agent"' in pyproject
    assert 'awen = "awen_agent.cli:main"' in pyproject
    assert 'version = { attr = "awen_agent.__version__" }' in pyproject
    assert 'include = ["awen_agent*"]' in pyproject


def test_project_identity_defaults_to_hidden_home(monkeypatch):
    """Without an override, all local state belongs under one hidden directory."""
    monkeypatch.delenv("AWEN_HOME", raising=False)

    from awen_agent import config

    importlib.reload(config)
    assert config.AWEN_DIR == Path.home() / ".awen"


def test_legacy_package_and_home_override_are_not_supported(monkeypatch, tmp_path):
    """The breaking rename must not silently keep a second runtime identity."""
    monkeypatch.delenv("AWEN_HOME", raising=False)
    monkeypatch.setenv(f"{LEGACY_STEM.upper()}_HOME", str(tmp_path))

    from awen_agent import config

    importlib.reload(config)
    assert config.AWEN_DIR == Path.home() / ".awen"
    assert importlib.util.find_spec(f"{LEGACY_STEM}_agent") is None

    canonical_home = tmp_path / "canonical"
    monkeypatch.setenv("AWEN_HOME", str(canonical_home))
    importlib.reload(config)
    assert config.AWEN_DIR == canonical_home


def test_repository_contains_no_legacy_identity():
    """Source, docs, deployment assets, and their paths use only the new identity."""
    ignored_parts = {".git", ".venv", ".pytest_cache", "__pycache__", "build", "dist"}
    legacy_bytes = LEGACY_STEM.encode("ascii")
    offenders: list[str] = []
    for path in ROOT.rglob("*"):
        relative = path.relative_to(ROOT)
        if any(part in ignored_parts for part in relative.parts):
            continue
        if LEGACY_STEM in relative.as_posix().lower():
            offenders.append(relative.as_posix())
            continue
        if not path.is_file():
            continue
        if legacy_bytes in path.read_bytes().lower():
            offenders.append(relative.as_posix())
    assert offenders == []
