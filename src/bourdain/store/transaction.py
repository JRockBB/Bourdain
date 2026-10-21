"""Recoverable multi-document updates for pantry/history events."""

import base64
import copy
import hashlib
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from bourdain.errors import BourdainError
from bourdain.store.repository import safe_path
from bourdain.store.yaml import YamlDocument, _patch, atomic_write, encode_yaml


@contextmanager
def _lock(root: Path) -> Iterator[None]:
    with (root / ".transaction.lock").open("a+b") as stream:
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise BourdainError(
                "Another pantry transaction is running; retry shortly"
            ) from None
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _replay(root: Path) -> None:
    journal = safe_path(root, ".transaction.json")
    if not journal.exists():
        return
    try:
        entries = json.loads(journal.read_bytes())
        if not isinstance(entries, list):
            raise ValueError
        changes = []
        for entry in entries:
            path = safe_path(root, entry["path"])
            after = base64.b64decode(entry["after"], validate=True)
            current = path.read_bytes()
            if _digest(current) not in {entry["before_hash"], _digest(after)}:
                raise BourdainError(
                    f"Transaction recovery conflicts with an external edit: {path}"
                )
            changes.append((path, current, after))
    except (ValueError, KeyError, TypeError, OSError):
        raise BourdainError(
            "Cannot recover pantry transaction; inspect .transaction.json"
        ) from None
    for path, before, after in changes:
        if before != after:
            atomic_write(path, after)
    journal.unlink()


def recover(root: Path) -> None:
    if not safe_path(root, ".transaction.json").exists():
        return
    with _lock(root):
        _replay(root)


def commit(root: Path, documents: list[tuple[YamlDocument[Any], Any, bool]]) -> None:
    """Stage round-trip changes, then publish with a roll-forward recovery journal."""
    root = root.resolve()
    with _lock(root):
        _replay(root)
        entries = []
        for document, model, automated in documents:
            after = document._adapter.dump_python(model, mode="json")
            raw = _patch(
                copy.deepcopy(document._raw), document._before, after, automated
            )
            validated = document._adapter.validate_python(raw)
            if (
                document._adapter.dump_python(validated, mode="json")
                == document._before
            ):
                continue
            path = safe_path(root, document.path.relative_to(root).as_posix())
            if path.read_bytes() != document._bytes:
                raise BourdainError(f"File changed since loading: {path}")
            entries.append(
                {
                    "path": path.relative_to(root).as_posix(),
                    "before_hash": _digest(document._bytes),
                    "after": base64.b64encode(encode_yaml(raw)).decode(),
                }
            )
        if entries:
            atomic_write(
                safe_path(root, ".transaction.json"),
                json.dumps(entries).encode(),
                overwrite=False,
            )
            _replay(root)
