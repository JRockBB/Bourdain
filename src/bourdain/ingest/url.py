"""Schema.org recipe extraction; the HTTP boundary is injectable and separate."""

from collections.abc import Callable
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from bourdain.errors import BourdainError
from bourdain.ingest.text import parse_servings
from bourdain.models import RecipeDraft, RecipeSource, RecipeTime
from bourdain.normalize.parse import parse_line


def fetch_html(url: str) -> str:
    if urlparse(url).scheme not in {"http", "https"}:
        raise BourdainError("Recipe URL must use http or https")
    try:
        request = Request(url, headers={"User-Agent": "Bourdain/0.1 recipe importer"})
        with urlopen(request, timeout=30) as response:
            content = response.read(5_000_001)
            if len(content) > 5_000_000:
                raise BourdainError("Recipe page exceeds 5 MB")
            return content.decode(response.headers.get_content_charset() or "utf-8")
    except (URLError, UnicodeError, TimeoutError) as exc:
        raise BourdainError("Could not download the recipe page") from exc


def from_html(html: str, url: str, *, store_steps: bool = False) -> RecipeDraft:
    from recipe_scrapers import scrape_html
    from recipe_scrapers._exceptions import RecipeScrapersExceptions

    try:
        scraper = scrape_html(html, org_url=url, supported_only=False)
        title = scraper.title()
        raw_ingredients = scraper.ingredients() or scraper.schema.ingredients()
        ingredients = [parse_line(line) for line in raw_ingredients]
    except (RecipeScrapersExceptions, ValueError, KeyError, TypeError) as exc:
        raise BourdainError(
            "No readable recipe metadata found; use ingest text instead"
        ) from exc
    # Some scrapers reduce ranges or counts ("4 to 6", "Makes 12") to a number.
    # Check the original yield so that ambiguity isn't lost before review.
    raw_yield = scraper.schema.data.get("recipeYield")
    yields = raw_yield if isinstance(raw_yield, list) else [raw_yield]
    values = [parse_servings(str(value)) for value in yields]
    servings = (
        values[0] if values and None not in values and len(set(values)) == 1 else None
    )
    try:
        total_min = scraper.total_time()
    except (RecipeScrapersExceptions, ValueError, TypeError):
        total_min = None
    steps = []
    if store_steps:
        try:
            steps = scraper.instructions_list()
        except RecipeScrapersExceptions:
            pass
    if not title or not ingredients:
        raise BourdainError("Recipe metadata is missing a title or ingredients")
    return RecipeDraft(
        title=title,
        servings=servings,
        ingredients=ingredients,
        steps=steps,
        time=RecipeTime(total_min=total_min),
        source=RecipeSource(kind="url", url=url),
        flags=[] if servings else ["missing_servings"],
    )


def from_url(
    url: str, *, fetch: Callable[[str], str] = fetch_html, store_steps: bool = False
) -> RecipeDraft:
    return from_html(fetch(url), url, store_steps=store_steps)
