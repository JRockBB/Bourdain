from pathlib import Path

import pytest

from bourdain.config import load_settings
from bourdain.errors import BourdainError


def test_defaults_without_file(tmp_path: Path) -> None:
    settings = load_settings(tmp_path / "missing")
    assert settings.ingest.match_threshold == 85
    assert settings.plan.lean == 0.3
    assert not settings.paths.data.exists()


def test_path_precedence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BOURDAIN_DATA")
    assert load_settings().paths.data == tmp_path / "kitchen"
    env = tmp_path / "env"
    env.mkdir()
    (env / "bourdain.toml").write_text(
        '[paths]\ndata = "elsewhere"\n[plan]\nlean = 0.7', encoding="utf-8"
    )
    monkeypatch.setenv("BOURDAIN_DATA", str(env))
    assert load_settings().paths.data == env
    assert load_settings().plan.lean == 0.7
    assert load_settings(tmp_path / "cli").paths.data == tmp_path / "cli"


@pytest.mark.parametrize(
    "content", ["[broken", "[plan]\nlean = 5", '[llm]\napi_key = "private-value"']
)
def test_malformed_config_is_concise_and_redacted(tmp_path: Path, content: str) -> None:
    (tmp_path / "bourdain.toml").write_text(content, encoding="utf-8")
    with pytest.raises(BourdainError) as error:
        load_settings(tmp_path)
    assert "\n" not in str(error.value)
    assert "private-value" not in str(error.value)


def test_env_keys_not_serialized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-secret")
    monkeypatch.setenv("FDC_API_KEY", "test-fdc-secret")
    settings = load_settings(tmp_path)
    assert settings.anthropic_api_key.get_secret_value() == "test-anthropic-secret"
    assert settings.fdc_api_key.get_secret_value() == "test-fdc-secret"
    assert "secret" not in settings.model_dump_json()
    assert "secret" not in repr(settings)
