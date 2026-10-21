import json
import stat
from pathlib import Path
from zipfile import ZipFile, ZipInfo

import pytest
from typer.testing import CliRunner

from bourdain.backup import create_backup, restore_backup
from bourdain.cli import app
from bourdain.errors import BourdainError
from bourdain.grocery.aggregate import GroceryItem, GroceryList
from bourdain.grocery.todoist import push_groceries
from bourdain.store import Repository, initialize


def contents(root: Path) -> dict[str, bytes]:
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in root.rglob("*")
        if p.is_file() and p.name != ".transaction.lock"
    }


def test_round_trip_bytes_and_retry_records(tmp_path: Path) -> None:
    repo = Repository(initialize(tmp_path / "data"))
    (repo.root / "pantry.yaml").write_text(
        '# Handwritten comment\nitems: []\napplied_purchases: {"trip:onion": 450}\n'
    )
    (repo.root / "raw" / "photo.png").write_bytes(b"original photo bytes")
    (repo.root / "notes.txt").write_text("keep user files too")
    (repo.root / "cronometer_exports.yaml").write_text("{}")
    calls = []

    class Provider:
        def send(self, commands):
            calls.append(commands)
            return {"sync_status": {command["uuid"]: "ok" for command in commands}}

    groceries = GroceryList(
        "2026-W43", [GroceryItem("onion", "Onion", "produce", need_g=320, buy_g=450)]
    )
    push_groceries(repo, groceries, "test", Provider())
    before = contents(repo.root)
    (repo.root / ".cache" / "unused.json").write_text("cache")
    (repo.root / ".env").write_text("FDC_API_KEY=do-not-copy")
    (repo.root / ".env.local").write_text("do-not-copy")
    (repo.root / ".envrc").write_text("do-not-copy")
    (repo.root / ".git").mkdir()
    (repo.root / ".git" / "config").write_text("do-not-copy")
    archive, destination = tmp_path / "backup.zip", tmp_path / "restored"
    result = create_backup(repo.root, archive)
    assert result.files == len(before)
    restore_backup(archive, destination)
    assert contents(destination) == before
    restored = Repository(destination)
    assert restored.pantry().model.applied_purchases == {"trip:onion": 450}
    repeated = push_groceries(restored, groceries, "test", Provider())
    assert repeated.already_sent == 1 and len(calls) == 1


def test_no_overwrites_nesting_or_pending_recovery(tmp_path: Path) -> None:
    root = initialize(tmp_path / "data")
    archive = tmp_path / "backup.zip"
    create_backup(root, archive)
    before = archive.read_bytes()
    with pytest.raises(BourdainError, match="already exists"):
        create_backup(root, archive)
    with pytest.raises(BourdainError, match="outside"):
        create_backup(root, root / "backup.zip")
    existing = tmp_path / "existing"
    existing.mkdir()
    with pytest.raises(BourdainError, match="already exists"):
        restore_backup(archive, existing)
    assert list(existing.iterdir()) == []
    (root / ".transaction.json").write_text("do not replay")
    before_root = contents(root)
    with pytest.raises(BourdainError, match="Pending transaction"):
        create_backup(root, tmp_path / "pending.zip")
    assert contents(root) == before_root
    assert archive.read_bytes() == before
    assert not (tmp_path / "pending.zip").exists()


def test_changed_snapshot_is_never_published(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import bourdain.backup as module

    root = initialize(tmp_path / "data")
    original = module._record

    def changed(path: Path):
        if path.name == "pantry.yaml":
            path.write_text("items: []\n# concurrent edit\n")
        return original(path)

    monkeypatch.setattr(module, "_record", changed)
    with pytest.raises(BourdainError, match="changed during backup"):
        create_backup(root, tmp_path / "backup.zip")
    assert not (tmp_path / "backup.zip").exists()
    assert not list(tmp_path.glob(".bourdain-backup-*"))


@pytest.mark.parametrize(
    "damage",
    [
        "checksum",
        "size",
        "missing",
        "extra",
        "traversal",
        "absolute",
        "backslash",
        "collision",
        "parent_collision",
        "link",
        "duplicate",
        "version",
        "pending",
        "env",
    ],
)
def test_damaged_or_unsafe_archives_leave_no_destination(
    tmp_path: Path, damage: str
) -> None:
    root = initialize(tmp_path / "data")
    archive = tmp_path / "good.zip"
    create_backup(root, archive)
    with ZipFile(archive) as original:
        payload = {name: original.read(name) for name in original.namelist()}
    manifest = json.loads(payload.pop("manifest.json"))
    record = manifest["files"]["pantry.yaml"].copy()
    extra = None
    if damage == "checksum":
        manifest["files"]["pantry.yaml"]["sha256"] = "0" * 64
    elif damage == "size":
        manifest["files"]["pantry.yaml"]["size"] += 1
    elif damage == "missing":
        payload.pop("data/pantry.yaml")
    elif damage == "extra":
        payload["data/extra"] = b"unlisted"
    elif damage in {
        "traversal",
        "absolute",
        "backslash",
        "collision",
        "parent_collision",
        "pending",
        "env",
    }:
        name = {
            "traversal": "../escape",
            "absolute": "/escape",
            "backslash": "raw\\escape",
            "collision": "PANTRY.yaml",
            "parent_collision": "pantry.yaml/child",
            "pending": ".transaction.json",
            "env": ".env",
        }[damage]
        manifest["files"][name] = record
        payload[f"data/{name}"] = payload["data/pantry.yaml"]
    elif damage == "link":
        extra = ZipInfo("data/pantry.yaml")
        extra.create_system = 3
        extra.external_attr = (stat.S_IFLNK | 0o777) << 16
    elif damage == "version":
        manifest["version"] = 9000
    corrupted = tmp_path / "bad.zip"
    with ZipFile(corrupted, "w") as output:
        output.writestr("manifest.json", json.dumps(manifest))
        for name, data in payload.items():
            output.writestr(
                extra if extra and name == "data/pantry.yaml" else name, data
            )
        if damage == "duplicate":
            with pytest.warns(UserWarning, match="Duplicate"):
                output.writestr("data/pantry.yaml", payload["data/pantry.yaml"])
    destination = tmp_path / "restored"
    with pytest.raises(BourdainError):
        restore_backup(corrupted, destination)
    assert not destination.exists()
    assert not (tmp_path / "escape").exists()
    assert not list(tmp_path.glob(".bourdain-restore-*"))


def test_symlinked_source_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = initialize(tmp_path / "data")
    original = Path.is_symlink
    monkeypatch.setattr(
        Path, "is_symlink", lambda p: p.name == "catalog" or original(p)
    )
    with pytest.raises(BourdainError, match="links"):
        create_backup(root, tmp_path / "backup.zip")


def test_cli_backup_restore(tmp_path: Path) -> None:
    root = initialize(tmp_path / "data")
    archive, restored = tmp_path / "backup.zip", tmp_path / "restored"
    runner = CliRunner()
    result = runner.invoke(app, ["--data", str(root), "backup", str(archive)])
    assert result.exit_code == 0, result.output
    result = runner.invoke(app, ["restore", str(archive), str(restored)])
    assert result.exit_code == 0, result.output
    assert contents(root) == contents(restored)
