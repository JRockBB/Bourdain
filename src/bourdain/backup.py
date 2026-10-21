"""Versioned, checksummed ZIP snapshots and exclusive separate-folder restore."""

import hashlib
import json
import os
import shutil
import stat
import tempfile
import zlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal
from zipfile import ZIP_DEFLATED, BadZipFile, ZipFile

from pydantic import Field, ValidationError

from bourdain.errors import BourdainError
from bourdain.models.schemas import Model
from bourdain.store.inventory import excluded, file_inventory, portable_name
from bourdain.store.transaction import _lock

MAX_FILES = 100_000
MAX_BYTES = 20 * 1024**3
MAX_MANIFEST_BYTES = 20 * 1024**2
CHUNK = 1024 * 1024
REQUIRED = {
    "catalog/ingredients.yaml",
    "pantry.yaml",
    "prices.yaml",
    "targets.yaml",
    "history.yaml",
}


class FileRecord(Model):
    size: Annotated[int, Field(ge=0)]
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class Manifest(Model):
    format: Literal["bourdain-backup"] = "bourdain-backup"
    version: Literal[1] = 1
    created_at: datetime
    files: dict[str, FileRecord]


class BackupResult(Model):
    path: str
    files: int
    bytes: int


def _record(path: Path) -> FileRecord:
    digest, size = hashlib.sha256(), 0
    with path.open("rb") as stream:
        while data := stream.read(CHUNK):
            digest.update(data)
            size += len(data)
    return FileRecord(size=size, sha256=digest.hexdigest())


def _pending(root: Path) -> None:
    if (root / ".transaction.json").exists():
        raise BourdainError("Pending transaction: recover it before making a backup")


def create_backup(root: Path, archive: Path) -> BackupResult:
    """Keep data bytes and retry records, omitting caches, VCS and .env files."""
    root, archive = root.expanduser().resolve(), archive.expanduser().absolute()
    if not root.is_dir():
        raise BourdainError("Initialize the data folder before making a backup")
    if archive.resolve().is_relative_to(root):
        raise BourdainError("Put the backup archive outside the data folder")
    if archive.exists() or archive.is_symlink():
        raise BourdainError("Backup archive already exists; choose a new filename")
    if not archive.parent.is_dir():
        raise BourdainError("Backup parent folder must already exist")
    _pending(root)
    lock_path = root / ".transaction.lock"
    if (
        lock_path.is_symlink()
        or lock_path.is_junction()
        or (lock_path.exists() and not lock_path.is_file())
    ):
        raise BourdainError("Transaction lock must be a regular file, not a link")
    with (
        _lock(root),
        tempfile.TemporaryDirectory(
            prefix=".bourdain-backup-", dir=archive.parent
        ) as temporary,
    ):
        _pending(root)
        names = file_inventory(root)
        if not REQUIRED <= set(names):
            raise BourdainError("Required library files are missing; run doctor first")
        if len(names) > MAX_FILES:
            raise BourdainError("Backup exceeds 100,000 files")
        staged = Path(temporary) / "backup.zip"
        records: dict[str, FileRecord] = {}
        total = 0
        with ZipFile(staged, "w", compression=ZIP_DEFLATED, allowZip64=True) as output:
            for name in names:
                digest, size = hashlib.sha256(), 0
                path = root / name
                before = path.stat()
                with (
                    path.open("rb") as source,
                    output.open(f"data/{name}", "w", force_zip64=True) as target,
                ):
                    while data := source.read(CHUNK):
                        size += len(data)
                        total += len(data)
                        if total > MAX_BYTES:
                            raise BourdainError("Backup exceeds 20 GiB")
                        digest.update(data)
                        target.write(data)
                after = path.stat()
                if (before.st_size, before.st_mtime_ns, before.st_ino) != (
                    after.st_size,
                    after.st_mtime_ns,
                    after.st_ino,
                ):
                    raise BourdainError(
                        "Library changed during backup; pause writers and retry"
                    )
                records[name] = FileRecord(size=size, sha256=digest.hexdigest())
            manifest = Manifest(created_at=datetime.now(UTC), files=records)
            content = manifest.model_dump_json(indent=2).encode()
            if len(content) > MAX_MANIFEST_BYTES:
                raise BourdainError("Backup manifest exceeds 20 MiB")
            output.writestr("manifest.json", content)
        # The lock guards transactions/Todoist. Other writers and hand edits are
        # detected by re-reading all included bytes and checking the inventory.
        if names != file_inventory(root) or any(
            _record(root / name) != record for name, record in records.items()
        ):
            raise BourdainError(
                "Library changed during backup; pause writers and retry"
            )
        _pending(root)
        with staged.open("r+b") as stream:
            os.fsync(stream.fileno())
        os.link(staged, archive)  # Atomic, exclusive publication; no overwrite race.
    return BackupResult(path=str(archive), files=len(records), bytes=total)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate manifest key")
        result[key] = value
    return result


def _manifest(archive: ZipFile) -> Manifest:
    entries = archive.infolist()
    names = [item.filename for item in entries]
    if len(entries) > MAX_FILES + 1 or len(set(names)) != len(names):
        raise BourdainError("Archive has duplicate entries or too many files")
    if "manifest.json" not in names:
        raise BourdainError("Not a Bourdain backup: missing manifest")
    if archive.getinfo("manifest.json").file_size > MAX_MANIFEST_BYTES:
        raise BourdainError("Backup manifest exceeds 20 MiB")
    manifest = Manifest.model_validate(
        json.loads(archive.read("manifest.json"), object_pairs_hook=_unique_object)
    )
    files = manifest.files
    if not REQUIRED <= files.keys():
        raise BourdainError("Backup is missing required library files")
    if any(
        not portable_name(name) or excluded(name) or name == ".transaction.json"
        for name in files
    ):
        raise BourdainError("Backup contains an unsafe or excluded path")
    folded = {name.casefold() for name in files}
    if len(folded) != len(files) or any(
        parent.as_posix().casefold() in folded
        for name in files
        for parent in Path(name).parents
        if parent != Path(".")
    ):
        raise BourdainError("Backup paths collide")
    if set(names) != {"manifest.json", *(f"data/{name}" for name in files)}:
        raise BourdainError("Archive contents do not match the manifest")
    if sum(record.size for record in files.values()) > MAX_BYTES:
        raise BourdainError("Backup exceeds 20 GiB")
    for entry in entries:
        mode = entry.external_attr >> 16
        if (
            entry.is_dir()
            or stat.S_IFMT(mode) not in {0, stat.S_IFREG}
            or entry.flag_bits & 1
        ):
            raise BourdainError("Backup contains a link, special or encrypted entry")
        if (
            entry.filename != "manifest.json"
            and entry.file_size != files[entry.filename.removeprefix("data/")].size
        ):
            raise BourdainError("Archive size does not match its manifest")
    return manifest


def restore_backup(archive: Path, destination: Path) -> BackupResult:
    """Verify every byte before publishing exclusively into a new directory."""
    archive = archive.expanduser().resolve()
    destination = destination.expanduser().absolute()
    if destination.exists() or destination.is_symlink():
        raise BourdainError("Restore destination already exists; choose a new folder")
    if not destination.parent.is_dir():
        raise BourdainError("Restore parent folder must already exist")
    with tempfile.TemporaryDirectory(
        prefix=".bourdain-restore-", dir=destination.parent
    ) as temporary:
        stage = Path(temporary)
        try:
            with ZipFile(archive) as source:
                manifest = _manifest(source)
                for name, record in manifest.files.items():
                    path = stage / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    digest, size = hashlib.sha256(), 0
                    with (
                        source.open(f"data/{name}") as incoming,
                        path.open("xb") as output,
                    ):
                        while data := incoming.read(CHUNK):
                            size += len(data)
                            if size > record.size:
                                raise BourdainError("Archive size exceeds its manifest")
                            digest.update(data)
                            output.write(data)
                        output.flush()
                        os.fsync(output.fileno())
                    if FileRecord(size=size, sha256=digest.hexdigest()) != record:
                        raise BourdainError(f"Backup checksum mismatch: {name}")
        except (
            BadZipFile,
            EOFError,
            ValidationError,
            ValueError,
            KeyError,
            NotImplementedError,
            RuntimeError,
            zlib.error,
        ):
            raise BourdainError(
                "Invalid, unsupported or damaged Bourdain backup"
            ) from None
        # copytree reserves the root with mkdir(exist_ok=False). Hard links publish
        # verified bytes exclusively, and remain after the staging directory closes.
        try:
            shutil.copytree(stage, destination, copy_function=os.link)
        except FileExistsError:
            raise BourdainError(
                "Restore destination already exists; choose a new folder"
            ) from None
        except OSError:
            raise BourdainError(
                "Restore could not finish; inspect the destination "
                "and retry into a new folder"
            ) from None
    return BackupResult(
        path=str(destination),
        files=len(manifest.files),
        bytes=sum(row.size for row in manifest.files.values()),
    )
