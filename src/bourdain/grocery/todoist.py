"""One-way Todoist grocery pushes with persisted, retry-safe Sync command UUIDs."""

import hashlib
import json
import os
import time
from collections.abc import Callable
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from uuid import uuid4

from pydantic import Field, TypeAdapter

from bourdain.config import SECTION_ORDER
from bourdain.errors import BourdainError
from bourdain.grocery.aggregate import GroceryList
from bourdain.grocery.export import _label, sections
from bourdain.models import Plan
from bourdain.models.schemas import Model, Slug
from bourdain.store import Repository
from bourdain.store.transaction import _lock
from bourdain.store.yaml import create_yaml, load_yaml


class PushBatch(Model):
    project: str
    week: str
    name: Slug
    payload_digest: str
    commands: list[dict[str, Any]]
    completed: list[str] = Field(default_factory=list)


class PushResult(Model):
    total: int
    sent: int = 0
    already_sent: int = 0
    preview: list[dict[str, str]] = Field(default_factory=list)


class TodoistProvider(Protocol):
    def send(self, commands: list[dict[str, Any]]) -> dict[str, Any]: ...


def http_send(commands: list[dict[str, Any]], token: str) -> dict[str, Any]:
    request = Request(
        "https://api.todoist.com/api/v1/sync",
        data=urlencode({"commands": json.dumps(commands)}).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    with urlopen(request, timeout=30) as response:
        data = response.read(10_000_001)
    if len(data) > 10_000_000:
        raise BourdainError("Todoist response exceeds 10 MB")
    return json.loads(data)


class TodoistClient:
    def __init__(
        self,
        *,
        fetch: Callable[..., Any] = http_send,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.fetch, self.sleep = fetch, sleep

    def send(self, commands: list[dict[str, Any]]) -> dict[str, Any]:
        token = os.environ.get("TODOIST_API_TOKEN")
        if not token:
            raise BourdainError("Set TODOIST_API_TOKEN to push groceries")
        for attempt in range(3):
            delay = 2**attempt
            try:
                result = self.fetch(commands, token)
                if not isinstance(result, dict) or not isinstance(
                    result.get("sync_status"), dict
                ):
                    raise BourdainError(
                        "Invalid Todoist response; retry the same batch"
                    )
                return result
            except HTTPError as exc:
                if exc.code not in {408, 409, 429} and exc.code < 500 or attempt == 2:
                    raise BourdainError(
                        f"Todoist request failed (HTTP {exc.code}); "
                        "retry the same batch"
                    ) from None
                try:
                    delay = float(exc.headers.get("Retry-After", delay))
                except (ValueError, TypeError):
                    pass
            except (URLError, TimeoutError, OSError):
                if attempt == 2:
                    raise BourdainError(
                        "Todoist connection failed; retry the same batch"
                    ) from None
            except (ValueError, UnicodeError):
                raise BourdainError(
                    "Invalid Todoist response; retry the same batch"
                ) from None
            self.sleep(max(0, min(delay, 30)))
        raise BourdainError("Todoist did not return a result")


def task_preview(
    groceries: GroceryList, section_order: list[str] | None = None
) -> list[dict[str, str]]:
    tasks = []
    for section, items in sections(
        groceries, SECTION_ORDER if section_order is None else section_order
    ):
        for item in items:
            tasks.append(
                {
                    "content": (
                        f"[{groceries.week}] {section.capitalize()}: {_label(item)}"
                    ),
                    "description": "\n".join(
                        [
                            f"Bourdain ingredient: {item.ingredient_id}",
                            *item.notes,
                            *groceries.warnings,
                        ]
                    ),
                }
            )
    return tasks


def push_groceries(
    repo: Repository,
    groceries: GroceryList,
    project: str,
    provider: TodoistProvider,
    *,
    batch: str = "weekly",
    dry_run: bool = False,
    section_order: list[str] | None = None,
) -> PushResult:
    """Push an immutable shopping snapshot; changed lists need a new explicit batch."""
    if not project.strip() or any(character.isspace() for character in project):
        raise BourdainError("Supply a Todoist project ID")
    TypeAdapter(Slug).validate_python(batch)
    Plan(week=groceries.week)
    tasks = task_preview(groceries, section_order)
    if dry_run:
        return PushResult(total=len(tasks), preview=tasks)
    repo.catalog()  # No API calls or export state before data-folder initialization.
    payload_digest = hashlib.sha256(
        json.dumps(tasks, sort_keys=True).encode()
    ).hexdigest()
    identity = hashlib.sha256(
        json.dumps([project, groceries.week, batch]).encode()
    ).hexdigest()
    path = repo.path(f"exports/todoist/{identity}.yaml")
    if not tasks and not path.exists():
        return PushResult(total=0)
    # Hold a process lock across staging/sending/acknowledgment, including retries.
    with _lock(repo.root):
        if not path.exists():
            commands = [
                {
                    "type": "item_add",
                    "uuid": str(uuid4()),
                    "temp_id": str(uuid4()),
                    "args": task | {"project_id": project},
                }
                for task in tasks
            ]
            create_yaml(
                path,
                PushBatch(
                    project=project,
                    week=groceries.week,
                    name=batch,
                    payload_digest=payload_digest,
                    commands=commands,
                ),
                PushBatch,
            )
        document = load_yaml(path, PushBatch)
        state = document.model
        if (state.project, state.week, state.name, state.payload_digest) != (
            project,
            groceries.week,
            batch,
            payload_digest,
        ):
            raise BourdainError(
                "Groceries changed after this batch was staged. Review the existing "
                "Todoist list and choose --batch NEW_NAME for a separate export."
            )
        # Validate the stored snapshot against this concrete export before any send.
        if len(state.commands) != len(tasks) or any(
            command.get("type") != "item_add"
            or command.get("args") != task | {"project_id": project}
            or not isinstance(command.get("uuid"), str)
            or not command["uuid"]
            or not isinstance(command.get("temp_id"), str)
            or not command["temp_id"]
            for command, task in zip(state.commands, tasks, strict=True)
        ):
            raise BourdainError(
                "Todoist export state does not match its grocery snapshot"
            )
        ids = [command["uuid"] for command in state.commands]
        if len(set(ids)) != len(ids) or not set(state.completed) <= set(ids):
            raise BourdainError("Invalid Todoist command tracking")
        pending = [
            command
            for command in state.commands
            if command["uuid"] not in state.completed
        ]
        result = PushResult(total=len(tasks), already_sent=len(tasks) - len(pending))
        for start in range(0, len(pending), 100):
            chunk = pending[start : start + 100]
            response = provider.send(chunk)
            statuses = (
                response.get("sync_status", {}) if isinstance(response, dict) else {}
            )
            if not isinstance(statuses, dict):
                raise BourdainError("Invalid Todoist response; retry the same batch")
            succeeded = [
                command["uuid"]
                for command in chunk
                if statuses.get(command["uuid"]) == "ok"
            ]
            state.completed = list(dict.fromkeys(state.completed + succeeded))
            state = document.save(state, automated=False)
            result.sent += len(succeeded)
            if len(succeeded) != len(chunk):
                raise BourdainError(
                    f"Todoist accepted {len(succeeded)} of {len(chunk)} tasks in this "
                    "request; retry the same batch to resume. Check the project ID "
                    "and account access."
                )
        return result
