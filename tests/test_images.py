import hashlib
import io
from datetime import date
from pathlib import Path

import pytest
from PIL import Image

from bourdain.errors import BourdainError
from bourdain.ingest.images import load_pages


@pytest.mark.parametrize("suffix", ["jpg", "png", "heic"])
def test_image_formats_and_originals(tmp_path: Path, suffix: str) -> None:
    if suffix == "heic":
        from pillow_heif import register_heif_opener

        register_heif_opener()
    path = tmp_path / f"input.{suffix}"
    Image.new("RGB", (2000, 1000), "red").save(path)
    original = path.read_bytes()
    root = tmp_path / "data"
    page = load_pages(path, root, today=date(2026, 10, 21))[0]
    assert (page.width, page.height) == (1600, 800)
    assert (root / page.original).read_bytes() == original
    assert hashlib.sha256(page.content).hexdigest() == page.content_hash
    assert page.original.startswith("raw/2026-10-21/")
    # A rerun on a later day reuses the same original and prepared bytes.
    assert load_pages(path, root, today=date(2026, 10, 22))[0] == page
    assert len(list((root / "raw").rglob(f"*.{suffix}"))) == 1


def test_exif_rotation_and_metadata_stripping(tmp_path: Path) -> None:
    path = tmp_path / "rotated.jpg"
    image = Image.new("RGB", (80, 40), "red")
    exif = Image.Exif()
    exif[274] = 6
    exif[306] = "2026:10:21 09:00:00"
    image.save(path, exif=exif)
    page = load_pages(path, tmp_path / "data")[0]
    assert (page.width, page.height) == (40, 80)
    assert page.captured_at.isoformat() == "2026-10-21T09:00:00"
    with Image.open(io.BytesIO(page.content)) as prepared:
        assert not prepared.getexif()


def test_pdf_page_selection(tmp_path: Path) -> None:
    import pymupdf

    path = tmp_path / "book.pdf"
    with pymupdf.open() as doc:
        for number in range(3):
            page = doc.new_page(width=300, height=400)
            page.insert_text((30, 30), f"Recipe page {number + 1}")
        doc.save(path)
    pages = load_pages(path, tmp_path / "data", page_numbers=[2, 3], max_edge=800)
    assert [page.page for page in pages] == [2, 3]
    assert all((page.width, page.height) == (600, 800) for page in pages)
    assert pages[0].original == pages[1].original
    assert pages[0].reference != pages[1].reference
    with pytest.raises(BourdainError, match="out of range"):
        load_pages(path, tmp_path / "data", page_numbers=[4])


def test_invalid_input_is_not_archived(tmp_path: Path) -> None:
    path = tmp_path / "broken.png"
    path.write_bytes(b"not an image")
    with pytest.raises(BourdainError, match="Cannot decode"):
        load_pages(path, tmp_path / "data")
    assert not (tmp_path / "data" / "raw").exists()
