"""Round-trip YAML documents, protected updates, and atomic file writes."""

import copy
import io
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter, ValidationError
from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from bourdain.errors import BourdainError


def protected(value: Any) -> bool:
    return isinstance(value, dict) and (
        value.get("locked") is True or value.get("source") == "user"
    )


def contains_protected(value: Any) -> bool:
    if protected(value):
        return True
    if isinstance(value, dict):
        return any(contains_protected(v) for v in value.values())
    if isinstance(value, list):
        return any(contains_protected(v) for v in value)
    return False


def merge_automated(current: Any, updates: Any) -> Any:
    """Merge partial updates while protecting user-owned values at any depth."""
    result = copy.deepcopy(current)
    if protected(current):
        return result
    if isinstance(current, dict) and isinstance(updates, dict):
        for key, value in updates.items():
            if key in current.get("user_fields", []) or (
                key == "user_fields" and current.get("user_fields")
            ):
                continue
            result[key] = merge_automated(current.get(key), value)
        return result
    if contains_protected(current):
        return result
    return copy.deepcopy(updates)


def atomic_write(path: Path, content: bytes, *, overwrite: bool = True) -> None:
    """Flush a sibling temporary file before publishing it atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if overwrite:
            os.replace(temporary, path)
        else:
            # An exclusive publish, without a check-then-overwrite race.
            os.link(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _yaml() -> YAML:
    yaml = YAML(typ="rt")
    yaml.preserve_quotes = True
    yaml.width = 1000
    return yaml


def encode_yaml(value: Any) -> bytes:
    stream = io.StringIO()
    _yaml().dump(value, stream)
    return stream.getvalue().encode("utf-8")


def _patch(current: Any, before: Any, after: Any, automated: bool) -> Any:
    if before == after or (automated and protected(current)):
        return current
    if (
        isinstance(current, dict)
        and isinstance(before, dict)
        and isinstance(after, dict)
    ):
        for key in before.keys() - after.keys():
            if automated and (
                key in current.get("user_fields", []) or key == "user_fields"
            ):
                continue
            if not automated or not contains_protected(current.get(key)):
                current.pop(key, None)
        for key, value in after.items():
            if automated and (
                key in current.get("user_fields", [])
                or (key == "user_fields" and current.get("user_fields"))
            ):
                continue
            if key not in before:
                current[key] = value
            elif before[key] != value:
                current[key] = _patch(current.get(key), before[key], value, automated)
        return current
    if (
        isinstance(current, list)
        and isinstance(before, list)
        and isinstance(after, list)
    ):
        if automated and contains_protected(current):
            return current
        if len(current) == len(before) == len(after):
            for index, value in enumerate(after):
                current[index] = _patch(current[index], before[index], value, automated)
            return current
    if automated and contains_protected(current):
        return current
    return copy.deepcopy(after)


@dataclass
class YamlDocument[T]:
    path: Path
    model: T
    _adapter: TypeAdapter[T]
    _raw: Any
    _bytes: bytes
    _before: Any

    def save(self, model: T | None = None, *, automated: bool = True) -> T:
        """Save changed fields only; reject a stale document instead of losing edits."""
        candidate = self.model if model is None else model
        after = self._adapter.dump_python(candidate, mode="json")
        raw = _patch(copy.deepcopy(self._raw), self._before, after, automated)
        validated = self._adapter.validate_python(raw)
        normalized = self._adapter.dump_python(validated, mode="json")
        if normalized == self._before:
            self.model = validated
            return validated
        if self.path.read_bytes() != self._bytes:
            raise BourdainError(
                f"File changed since loading: {self.path}; reload before saving"
            )
        content = encode_yaml(raw)
        atomic_write(self.path, content)
        self.model, self._raw, self._bytes, self._before = (
            validated,
            raw,
            content,
            normalized,
        )
        return validated


def load_yaml[T](path: Path, schema: type[T] | Any) -> YamlDocument[T]:
    try:
        content = path.read_bytes()
        raw = _yaml().load(content.decode("utf-8-sig"))
        adapter = TypeAdapter(schema)
        model = adapter.validate_python(raw)
    except (OSError, UnicodeError, YAMLError, ValidationError) as exc:
        if isinstance(exc, ValidationError):
            issue = exc.errors(include_input=False)[0]
            detail = ".".join(str(x) for x in issue["loc"]) or "document"
            reason = f"invalid {detail} ({issue['type']})"
        else:
            reason = "unreadable file or invalid YAML"
        raise BourdainError(f"Cannot load {path}: {reason}") from exc
    return YamlDocument(
        path, model, adapter, raw, content, adapter.dump_python(model, mode="json")
    )


def create_yaml[T](path: Path, model: T, schema: type[T] | Any) -> None:
    adapter = TypeAdapter(schema)
    validated = adapter.validate_python(model)
    value = adapter.dump_python(validated, mode="json", exclude_defaults=True)
    atomic_write(path, encode_yaml(value), overwrite=False)
