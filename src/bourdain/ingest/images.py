"""Prepare cookbook images while retaining content-addressed originals."""

import hashlib
import io
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

from bourdain.errors import BourdainError
from bourdain.store.repository import safe_path
from bourdain.store.yaml import atomic_write

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".heic", ".heif"}
SUPPORTED_SUFFIXES = IMAGE_SUFFIXES | {".pdf"}


@dataclass(frozen=True)
class PageImage:
    original: str
    page: int
    width: int
    height: int
    content: bytes = field(repr=False)
    captured_at: datetime | None = None

    @property
    def reference(self) -> str:
        return f"{self.original}#page={self.page}"

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.content).hexdigest()


def _jpeg(image: Image.Image, max_edge: int) -> tuple[bytes, int, int]:
    image = ImageOps.exif_transpose(image)
    image.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
    rgba = image.convert("RGBA")
    background = Image.new("RGBA", rgba.size, "white")
    background.alpha_composite(rgba)
    stream = io.BytesIO()
    # A new image removes EXIF, GPS and other metadata from the API payload.
    background.convert("RGB").save(stream, format="JPEG", quality=90)
    return stream.getvalue(), image.width, image.height


def _preserve(root: Path, content: bytes, suffix: str, today: date) -> str:
    digest = hashlib.sha256(content).hexdigest()
    name = f"{digest}{suffix}"
    raw = safe_path(root, "raw")
    for candidate in sorted(raw.glob(f"*/{name}")):
        checked = safe_path(root, candidate.relative_to(root).as_posix())
        if checked.read_bytes() != content:
            raise BourdainError(f"Original archive is inconsistent: {checked.name}")
        return checked.relative_to(root).as_posix()
    target = safe_path(root, f"raw/{today.isoformat()}/{name}")
    try:
        atomic_write(target, content, overwrite=False)
    except FileExistsError:
        if target.read_bytes() != content:
            raise BourdainError("Original archive changed during import") from None
    return target.relative_to(root).as_posix()


def load_pages(
    path: Path,
    root: Path,
    *,
    max_edge: int = 1600,
    page_numbers: list[int] | None = None,
    today: date | None = None,
) -> list[PageImage]:
    """Load JPEG/PNG/HEIC or selected PDF pages (one-based page numbers)."""
    if not 64 <= max_edge <= 4096:
        raise BourdainError("Image long edge must be between 64 and 4096 pixels")
    root = root.resolve()
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise BourdainError(f"Unsupported image type: {suffix}")
    if path.stat().st_size > 100_000_000:
        raise BourdainError("Input exceeds 100 MB; select or split the relevant pages")
    content = path.read_bytes()
    prepared: list[tuple[bytes, int, int, int]] = []
    captured_at = None
    try:
        if suffix == ".pdf":
            import pymupdf

            with pymupdf.open(stream=content, filetype="pdf") as document:
                if document.needs_pass:
                    raise BourdainError(
                        "PDF is password protected; provide an unlocked copy"
                    )
                numbers = (
                    page_numbers
                    if page_numbers is not None
                    else list(range(1, len(document) + 1))
                )
                if not numbers or len(numbers) > 100:
                    raise BourdainError("Select between 1 and 100 PDF pages")
                if len(set(numbers)) != len(numbers) or any(
                    n < 1 or n > len(document) for n in numbers
                ):
                    raise BourdainError(
                        "PDF page selection is duplicated or out of range"
                    )
                for number in numbers:
                    page = document[number - 1]
                    scale = max_edge / max(page.rect.width, page.rect.height)
                    pixmap = page.get_pixmap(
                        matrix=pymupdf.Matrix(scale, scale), alpha=False
                    )
                    with Image.open(io.BytesIO(pixmap.tobytes("png"))) as image:
                        data, width, height = _jpeg(image, max_edge)
                    prepared.append((data, width, height, number))
        else:
            if page_numbers is not None:
                raise BourdainError("Page selection applies only to PDF input")
            if suffix in {".heic", ".heif"}:
                from pillow_heif import register_heif_opener

                register_heif_opener(thumbnails=False)
            with Image.open(io.BytesIO(content)) as image:
                stamp = image.getexif().get(36867) or image.getexif().get(306)
                if stamp:
                    try:
                        captured_at = datetime.strptime(str(stamp), "%Y:%m:%d %H:%M:%S")
                    except ValueError:
                        pass
                data, width, height = _jpeg(image, max_edge)
            prepared.append((data, width, height, 1))
    except BourdainError:
        raise
    except (
        OSError,
        ValueError,
        RuntimeError,
        UnidentifiedImageError,
        Image.DecompressionBombError,
    ) as exc:
        raise BourdainError(f"Cannot decode cookbook input: {path.name}") from exc
    original = _preserve(root, content, suffix, today or date.today())
    return [
        PageImage(original, number, width, height, data, captured_at)
        for data, width, height, number in prepared
    ]
