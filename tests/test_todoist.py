import copy
import json
from pathlib import Path
from urllib.error import HTTPError

import pytest
from typer.testing import CliRunner

from bourdain.cli import app
from bourdain.errors import BourdainError
from bourdain.grocery.aggregate import GroceryItem, GroceryList
from bourdain.grocery.todoist import TodoistClient, push_groceries
from bourdain.ingest.text import from_text
from bourdain.library import import_draft
from bourdain.models import Targets
from bourdain.plan.manual import manual_plan
from bourdain.store import Repository, initialize


class FakeTodoist:
    def __init__(self, fail: int = 0) -> None:
        self.fail, self.calls = fail, []

    def send(self, commands):
        self.calls.append(copy.deepcopy(commands))
        return {
            "sync_status": {
                command["uuid"]: {"error": "private response text"}
                if index < self.fail
                else "ok"
                for index, command in enumerate(commands)
            }
        }


def shopping(count=2):
    return GroceryList(
        "2026-W43",
        [
            GroceryItem(
                f"food-{index}", f"Food {index}", "produce", buy_g=200, recipes=["Soup"]
            )
            for index in range(count)
        ],
    )


def test_preview_no_writes_and_repeat_push_no_duplicate_requests(
    tmp_path: Path,
) -> None:
    repo = Repository(initialize(tmp_path / "data"))
    provider = FakeTodoist()
    result = push_groceries(repo, shopping(), "project1", provider, dry_run=True)
    assert result.total == 2 and "200 g" in result.preview[0]["content"]
    assert not provider.calls and not repo.path("exports").exists()
    result = push_groceries(repo, shopping(), "project1", provider)
    assert result.sent == 2
    state = next(repo.path("exports/todoist").glob("*.yaml"))
    before = state.read_bytes()
    result = push_groceries(repo, shopping(), "project1", provider)
    assert result.sent == 0 and result.already_sent == 2
    assert len(provider.calls) == 1 and state.read_bytes() == before
    changed = shopping()
    changed.items[0].buy_g = 500
    with pytest.raises(BourdainError, match="Groceries changed"):
        push_groceries(repo, changed, "project1", provider)
    assert len(provider.calls) == 1
    with pytest.raises(BourdainError, match="Groceries changed"):
        push_groceries(repo, shopping(0), "project1", provider)
    assert (
        push_groceries(repo, changed, "project1", provider, batch="second-trip").sent
        == 2
    )


def test_partial_failure_resumes_only_pending_with_same_uuids(tmp_path: Path) -> None:
    repo = Repository(initialize(tmp_path / "data"))
    provider = FakeTodoist(fail=1)
    with pytest.raises(BourdainError, match="accepted 1 of 2") as error:
        push_groceries(repo, shopping(), "project1", provider)
    assert "private response" not in str(error.value)
    pending = provider.calls[0][0]
    provider.fail = 0
    result = push_groceries(repo, shopping(), "project1", provider)
    assert result.sent == 1 and result.already_sent == 1
    assert provider.calls[1] == [pending]


def test_lost_ack_reuses_persisted_commands(tmp_path: Path, monkeypatch) -> None:
    from bourdain.store.yaml import YamlDocument

    repo = Repository(initialize(tmp_path / "data"))
    provider = FakeTodoist()
    original = YamlDocument.save

    def interrupt(self, *args, **kwargs):
        raise OSError("simulated local write failure")

    with monkeypatch.context() as patch:
        patch.setattr(YamlDocument, "save", interrupt)
        with pytest.raises(OSError):
            push_groceries(repo, shopping(), "project1", provider)
    assert YamlDocument.save == original
    push_groceries(repo, shopping(), "project1", provider)
    assert provider.calls[0] == provider.calls[1]  # Server deduplicates command UUIDs.


def test_todoist_chunking(tmp_path: Path) -> None:
    repo = Repository(initialize(tmp_path / "data"))
    provider = FakeTodoist()
    result = push_groceries(repo, shopping(105), "project1", provider)
    assert result.sent == 105
    assert [len(commands) for commands in provider.calls] == [100, 5]
    assert push_groceries(repo, shopping(105), "project1", provider).already_sent == 105
    assert len(provider.calls) == 2


def test_http_retries_and_secrets(tmp_path: Path, monkeypatch) -> None:
    attempts, sleeps = [], []

    def fetch(commands, token):
        attempts.append(copy.deepcopy(commands))
        assert token == "private-test-token"
        if len(attempts) < 3:
            raise HTTPError(
                "private-url", 429, "private-body", {"Retry-After": "99"}, None
            )
        return {"sync_status": {command["uuid"]: "ok" for command in commands}}

    repo = Repository(initialize(tmp_path / "data"))
    client = TodoistClient(fetch=fetch, sleep=sleeps.append)
    with pytest.raises(BourdainError, match="TODOIST_API_TOKEN"):
        push_groceries(repo, shopping(), "project1", client)
    monkeypatch.setenv("TODOIST_API_TOKEN", "private-test-token")
    assert push_groceries(repo, shopping(), "project1", client).sent == 2
    assert sleeps == [30, 30] and attempts[0] == attempts[-1]
    for path in repo.path("exports").rglob("*.yaml"):
        assert "private-test-token" not in path.read_text()


def test_push_cli_preview_and_injected_provider(tmp_path: Path, monkeypatch) -> None:
    repo = Repository(initialize(tmp_path / "data"))
    recipe = import_draft(repo, from_text("Soup\nServings: 1\n100 g carrot"))
    repo.save_plan(
        manual_plan(
            "2026-W43",
            [f"mon dinner={recipe.id}"],
            {recipe.id: recipe},
            Targets(days=["mon"]),
        )
    )
    provider = FakeTodoist()
    monkeypatch.setattr("bourdain.cli.make_todoist", lambda: provider)
    runner = CliRunner()
    args = [
        "--data",
        str(repo.root),
        "push",
        "todoist",
        "--project",
        "project1",
        "--week",
        "2026-W43",
    ]
    result = runner.invoke(app, [*args, "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "100 g" in json.loads(result.stdout)[0]["content"]
    assert not provider.calls
    result = runner.invoke(app, args)
    assert result.exit_code == 0 and "1 sent" in result.output
