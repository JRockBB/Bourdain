"""Smoke test for the installed command-line application."""

import os
import subprocess
import sys
from pathlib import Path

import pytest


def test_cli_help(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The console script works without keys, config, or a data folder."""
    data_dir = tmp_path / "kitchen"
    monkeypatch.setenv("BOURDAIN_DATA", str(data_dir))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("FDC_API_KEY", raising=False)
    executable = Path(sys.executable).with_name(
        "bourdain.exe" if os.name == "nt" else "bourdain"
    )

    result = subprocess.run(
        [str(executable), "--help"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "Usage:" in result.stdout
    assert "bourdain" in result.stdout
    assert "A local-first meal planning toolkit." in result.stdout
    assert not data_dir.exists()
