import json
import zipfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from bourdain.cli import app
from bourdain.errors import BourdainError
from bourdain.ingest.epub import extract_epub
from bourdain.store import Repository, initialize


def make_book(
    path: Path, *, bad_href: str | None = None, encrypted: bool = False
) -> None:
    record = {
        "@type": "Recipe",
        "name": "Bean bowl",
        "recipeYield": "2 servings",
        "recipeIngredient": ["200 g beans"],
        "recipeInstructions": [{"@type": "HowToStep", "text": "Cook beans."}],
    }
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr(
            "META-INF/container.xml",
            '<container><rootfiles><rootfile full-path="OPS/book.opf" '
            'media-type="application/oebps-package+xml"/></rootfiles></container>',
        )
        href = bad_href or "one.xhtml"
        archive.writestr(
            "OPS/book.opf",
            f'<package><manifest><item id="one" href="{href}" '
            'media-type="application/xhtml+xml"/><item id="two" href="two.xhtml" '
            'media-type="application/xhtml+xml"/></manifest><spine>'
            '<itemref idref="two"/><itemref idref="one"/></spine></package>',
        )
        archive.writestr(
            "OPS/one.xhtml",
            '<html xmlns="http://www.w3.org/1999/xhtml"><body>'
            f'<script type="application/ld+json">{json.dumps({"@graph": [record]})}'
            "</script></body></html>",
        )
        archive.writestr(
            "OPS/two.xhtml",
            '<html xmlns="http://www.w3.org/1999/xhtml"><body>'
            "<h1>Onion Soup</h1><p>Serves 2</p>"
            "<p>A headnote that is not an ingredient.</p><h2>Ingredients</h2>"
            "<ul><li>1 medium onion</li><li>100 g beans</li></ul>"
            "<h2>Method</h2><p>Simmer.</p><h1>Quick Salad</h1>"
            "<h2>Ingredients</h2><p>20 g leaves</p></body></html>",
        )
        if encrypted:
            archive.writestr(
                "META-INF/encryption.xml",
                "<encryption><EncryptedData><CipherData>"
                '<CipherReference URI="OPS/two.xhtml"/>'
                "</CipherData></EncryptedData></encryption>",
            )


def test_spine_multiple_recipes_archive_and_repeat_import(tmp_path: Path) -> None:
    root = initialize(tmp_path / "data")
    book = tmp_path / "book.epub"
    make_book(book)
    result = extract_epub(book, root, book="my-book", store_steps=True)
    assert [draft.title for draft in result.drafts] == [
        "Onion Soup",
        "Quick Salad",
        "Bean bowl",
    ]
    assert result.drafts[0].servings == 2
    assert result.drafts[0].steps == ["Simmer."]
    assert len(result.drafts[0].ingredients) == 2
    assert result.drafts[1].servings is None
    assert "missing_servings" in result.drafts[1].flags
    assert result.drafts[2].steps == ["Cook beans."]
    assert result.drafts[0].source.section == "OPS/two.xhtml#recipe-1"
    assert (root / result.drafts[0].source.document).read_bytes() == book.read_bytes()
    runner = CliRunner()
    args = ["--data", str(root), "ingest", "epub", str(book), "--book", "my-book"]
    for _ in range(2):
        imported = runner.invoke(app, args)
        assert imported.exit_code == 0, imported.output
    assert len(Repository(root).recipes()) == 3
    assert len(list((root / "raw").rglob("*.epub"))) == 1
    assert Repository(root).recipe("my-book/onion-soup").model.source.kind == "epub"


@pytest.mark.parametrize(
    "href",
    ["../../outside.xhtml", "https://example.com/recipe.xhtml", "/tmp/recipe.xhtml"],
)
def test_invalid_archive_references_never_escape(tmp_path: Path, href: str) -> None:
    book = tmp_path / "bad.epub"
    make_book(book, bad_href=href)
    with pytest.raises(BourdainError, match="archive|local"):
        extract_epub(book, tmp_path / "data", book="test")
    assert not (tmp_path / "data").exists()


def test_encrypted_chapter_and_non_epub_refused(tmp_path: Path) -> None:
    book = tmp_path / "book.epub"
    make_book(book, encrypted=True)
    with pytest.raises(BourdainError, match="Encrypted"):
        extract_epub(book, tmp_path / "data", book="test")
    book.write_bytes(b"not a zip")
    with pytest.raises(BourdainError, match="Cannot read"):
        extract_epub(book, tmp_path / "data", book="test")
