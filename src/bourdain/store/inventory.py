"""Non-following file inventory shared by diagnostics and portable backups."""

import os
from collections.abc import Iterator
from pathlib import Path, PurePosixPath

from bourdain.errors import BourdainError

EXCLUDED = {".cache", ".git", "__pycache__", ".transaction.lock"}


def portable_name(name: str) -> bool:
    """Accept relative names that have the same meaning on Windows and POSIX."""
    parts = name.split("/")
    reserved = {"con", "prn", "aux", "nul"} | {
        f"{prefix}{number}" for prefix in ("com", "lpt") for number in range(1, 10)
    }
    return bool(name) and all(
        part not in {"", ".", ".."}
        and not part.endswith((".", " "))
        and not any(ord(c) < 32 or c in '\\:<>"|?*' for c in part)
        and part.split(".")[0].casefold() not in reserved
        for part in parts
    )


def excluded(name: str) -> bool:
    return any(
        part.casefold() in EXCLUDED or part.casefold().startswith(".env")
        for part in PurePosixPath(name).parts
    )


def inventory(root: Path) -> Iterator[tuple[str, str | None]]:
    """Yield (relative file, problem), without following links or junctions."""

    def visit(directory: Path) -> Iterator[tuple[str, str | None]]:
        try:
            entries = sorted(directory.iterdir())
        except OSError:
            yield directory.relative_to(root).as_posix(), "Cannot read directory"
            return
        for path in entries:
            name = path.relative_to(root).as_posix()
            if excluded(name):
                continue
            try:
                if path.is_symlink() or path.is_junction():
                    yield name, "Symbolic links and junctions are not supported"
                elif not portable_name(name):
                    yield name, "Filename is not portable between platforms"
                elif path.is_dir():
                    yield from visit(path)
                elif path.is_file():
                    yield name, None
                else:
                    yield name, "Not a regular file"
            except OSError:
                yield name, "Cannot inspect path"

    yield from visit(root)


def file_inventory(root: Path) -> list[str]:
    names = []
    for name, problem in inventory(root):
        if problem:
            raise BourdainError(f"{name}: {problem}")
        names.append(name)
    if len({os.path.normcase(name).casefold() for name in names}) != len(names):
        raise BourdainError("File names collide on a case-insensitive filesystem")
    return names
