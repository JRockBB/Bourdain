"""Read bounded, unencrypted EPUB XHTML in spine order without network calls."""

import io
import json
import posixpath
import re
import zipfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree as ET

from bourdain.errors import BourdainError
from bourdain.ingest.images import _preserve
from bourdain.ingest.text import from_text, parse_servings
from bourdain.models import RecipeDraft, RecipeSource
from bourdain.normalize.catalog import slugify
from bourdain.normalize.parse import parse_line


@dataclass
class EPUBResult:
    drafts: list[RecipeDraft]
    warnings: list[str]


def _local(base: str, href: str) -> str:
    url = urlsplit(href)
    path = unquote(url.path)
    if (
        url.scheme
        or url.netloc
        or path.startswith("/")
        or "\\" in path
        or "\x00" in path
    ):
        raise BourdainError("EPUB reference must name a local archive member")
    resolved = posixpath.normpath(posixpath.join(base, path))
    if resolved == ".." or resolved.startswith("../"):
        raise BourdainError("EPUB reference escapes the archive")
    return resolved


def _xml(content: bytes) -> ET.Element:
    if re.search(rb"<!\s*ENTITY", content, re.I) or b"\x00" in content:
        raise BourdainError("EPUB uses unsupported XML entities or encoding")
    try:
        return ET.fromstring(content)
    except ET.ParseError:
        raise BourdainError("EPUB contains invalid XML/XHTML") from None


def _name(element: ET.Element) -> str:
    return element.tag.rsplit("}", 1)[-1] if isinstance(element.tag, str) else ""


def _text(element: ET.Element) -> str:
    return " ".join("".join(element.itertext()).split())


def _json_recipes(value: object) -> list[dict]:
    if isinstance(value, list):
        return [recipe for child in value for recipe in _json_recipes(child)]
    if isinstance(value, dict):
        types = value.get("@type", [])
        if isinstance(types, str):
            types = [types]
        if "Recipe" in types:
            return [value]
        return [recipe for child in value.values() for recipe in _json_recipes(child)]
    return []


def _instructions(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [step for child in value for step in _instructions(child)]
    if isinstance(value, dict):
        return _instructions(value.get("text") or value.get("itemListElement", []))
    return []


def _chapter(root: ET.Element, *, store_steps: bool) -> list[RecipeDraft]:
    structured = []
    for element in root.iter():
        if (
            _name(element) == "script"
            and element.get("type", "").lower() == "application/ld+json"
        ):
            try:
                records = _json_recipes(json.loads(element.text or ""))
            except (ValueError, RecursionError):
                raise BourdainError(
                    "EPUB recipe contains invalid JSON metadata"
                ) from None
            for record in records:
                title, ingredients = record.get("name"), record.get("recipeIngredient")
                if (
                    not isinstance(title, str)
                    or not isinstance(ingredients, list)
                    or not ingredients
                    or not all(isinstance(line, str) for line in ingredients)
                ):
                    raise BourdainError(
                        "EPUB recipe metadata needs a title and ingredient lines"
                    )
                raw_yield = record.get("recipeYield")
                yields = raw_yield if isinstance(raw_yield, list) else [raw_yield]
                values = [parse_servings(str(value)) for value in yields]
                servings = (
                    values[0]
                    if values and None not in values and len(set(values)) == 1
                    else None
                )
                structured.append(
                    RecipeDraft(
                        title=title,
                        servings=servings,
                        ingredients=[parse_line(line) for line in ingredients],
                        steps=_instructions(record.get("recipeInstructions"))
                        if store_steps
                        else [],
                        flags=[] if servings else ["missing_servings"],
                    )
                )
    if structured:
        return structured
    # Only explicit Ingredients sections become recipes; prose and navigation
    # pages are never interpreted as ingredient lists.
    body = next((element for element in root.iter() if _name(element) == "body"), root)
    blocks: list[tuple[str, str]] = []

    def walk(element: ET.Element) -> None:
        tag = _name(element)
        if tag in {"script", "style", "nav"}:
            return
        if tag in {"h1", "h2", "h3", "h4", "p", "li"}:
            text = _text(element)
            if text:
                blocks.append((tag, text))
        else:
            for child in element:
                walk(child)

    walk(body)
    drafts, lines = [], []
    section = "intro"
    has_ingredients = False

    def finish() -> None:
        if lines and has_ingredients:
            drafts.append(from_text("\n".join(lines), store_steps=store_steps))

    for tag, text in blocks:
        heading = text.strip(": ").casefold()
        if heading in {"ingredients", "instructions", "directions", "method", "steps"}:
            if not lines:
                continue
            section = "ingredients" if heading == "ingredients" else "steps"
            has_ingredients |= section == "ingredients"
            lines.append(
                "Ingredients:" if section == "ingredients" else "Instructions:"
            )
        elif tag in {"h1", "h2"}:
            finish()
            lines, section, has_ingredients = [text], "intro", False
        elif lines and re.match(r"^(servings?|serves|yield)\b", text, re.I):
            lines.append(text)
        elif lines and section in {"ingredients", "steps"}:
            lines.append(
                text.rstrip(":") + ":"
                if tag in {"h3", "h4"} and section == "ingredients"
                else text
            )
    finish()
    return drafts


def extract_epub(
    path: Path, root: Path, *, book: str, store_steps: bool = False
) -> EPUBResult:
    if path.suffix.lower() != ".epub" or path.stat().st_size > 100_000_000:
        raise BourdainError("Supply an EPUB file no larger than 100 MB")
    content = path.read_bytes()
    drafts, warnings = [], []
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            entries = archive.infolist()
            if (
                len(entries) > 5000
                or sum(entry.file_size for entry in entries) > 200_000_000
            ):
                raise BourdainError("EPUB exceeds archive limits")
            if len({entry.filename for entry in entries}) != len(entries):
                raise BourdainError("EPUB contains duplicate archive members")

            def read(name: str) -> bytes:
                entry = archive.getinfo(name)
                if entry.flag_bits & 1 or entry.file_size > 5_000_000:
                    raise BourdainError(
                        "Encrypted or oversized EPUB chapter is unsupported"
                    )
                return archive.read(entry)

            if read("mimetype").strip() != b"application/epub+zip":
                raise BourdainError("Archive is not an EPUB publication")
            container = _xml(read("META-INF/container.xml"))
            opf = next(
                (
                    element.get("full-path")
                    for element in container.iter()
                    if _name(element) == "rootfile"
                    and element.get("media-type") == "application/oebps-package+xml"
                ),
                None,
            )
            if not opf:
                raise BourdainError("EPUB has no package document")
            opf = _local("", opf)
            package = _xml(read(opf))
            manifest = {
                element.get("id"): element
                for element in package.iter()
                if _name(element) == "item"
            }
            encrypted = set()
            if "META-INF/encryption.xml" in archive.namelist():
                encryption = _xml(read("META-INF/encryption.xml"))
                encrypted = {
                    _local("", element.get("URI", ""))
                    for element in encryption.iter()
                    if _name(element) == "CipherReference"
                }
            spine = [
                element
                for element in package.iter()
                if _name(element) == "itemref" and element.get("linear", "yes") != "no"
            ]
            if len(spine) > 500:
                raise BourdainError("EPUB exceeds 500 reading-order chapters")
            seen = set()
            for entry in spine:
                item = manifest.get(entry.get("idref"))
                if item is None:
                    raise BourdainError(
                        "EPUB spine references a missing manifest entry"
                    )
                member = _local(posixpath.dirname(opf), item.get("href", ""))
                if member in seen:
                    continue
                seen.add(member)
                if member in encrypted:
                    raise BourdainError(
                        "Encrypted EPUB text is unsupported; use an unencrypted export"
                    )
                if item.get("media-type") != "application/xhtml+xml":
                    warnings.append(f"Skipped non-XHTML chapter: {member}")
                    continue
                extracted = _chapter(_xml(read(member)), store_steps=store_steps)
                if not extracted:
                    warnings.append(f"No explicit recipe found: {member}")
                for index, draft in enumerate(extracted, 1):
                    draft.source = RecipeSource(
                        kind="epub",
                        book=slugify(book),
                        section=f"{member}#recipe-{index}",
                    )
                    drafts.append(draft)
                    if len(drafts) > 1000:
                        raise BourdainError("EPUB exceeds 1000 recipes")
    except BourdainError:
        raise
    except (
        zipfile.BadZipFile,
        KeyError,
        UnicodeError,
        RuntimeError,
        NotImplementedError,
        ValueError,
    ) as exc:
        raise BourdainError("Cannot read EPUB archive or recipe data") from exc
    if not drafts:
        raise BourdainError(
            "No recipes found: EPUB needs Recipe JSON metadata or "
            "titled sections with an Ingredients heading"
        )
    titles = [slugify(draft.title) for draft in drafts]
    if len(set(titles)) != len(titles):
        raise BourdainError(
            "EPUB has duplicate recipe titles; split or rename them before import"
        )
    original = _preserve(root, content, ".epub", date.today())
    for draft in drafts:
        draft.source.document = original
    return EPUBResult(drafts, warnings)
