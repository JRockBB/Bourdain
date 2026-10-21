"""Isolated data folders and offline tests by default."""

import socket
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--update-golden", action="store_true", help="Update expected text outputs"
    )


@pytest.fixture(autouse=True)
def isolated_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    monkeypatch.setenv("BOURDAIN_DATA", str(tmp_path / "kitchen"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    if request.node.get_closest_marker("live") is None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("FDC_API_KEY", raising=False)

        def no_network(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("Network access is forbidden in offline tests")

        monkeypatch.setattr(socket.socket, "connect", no_network)
        monkeypatch.setattr(socket.socket, "connect_ex", no_network)
        monkeypatch.setattr(socket, "getaddrinfo", no_network)


@pytest.fixture
def golden(request: pytest.FixtureRequest) -> Callable[[str, str], None]:
    def check(name: str, actual: str) -> None:
        path = Path(__file__).parent / "golden" / name
        if request.config.getoption("--update-golden"):
            path.write_text(actual, encoding="utf-8", newline="\n")
        assert actual == path.read_text(encoding="utf-8")

    return check
