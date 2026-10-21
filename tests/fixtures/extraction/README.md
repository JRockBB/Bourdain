# Extraction fixtures

`single.json` and `seven-pages.json` are hand-authored fake responses, not captured
Anthropic responses. Image tests generate JPEG, PNG, HEIC, EXIF-rotated photos and
PDF files in temporary directories. The seven-page batch exercises continuation
merging and multiple recipes per page without network access.

The real-page acceptance test needs user-supplied pages that can be sent to
Anthropic, plus `ANTHROPIC_API_KEY`. Set `BOURDAIN_LIVE_PAGES` to an absolute JSON
manifest path containing these three cases:

```json
[
  {
    "case": "two-page",
    "files": ["page1.jpg", "page2.jpg"],
    "recipes": [{"title": "Soup", "servings": 2, "ingredients": ["100 g carrots"]}]
  },
  {
    "case": "two-recipes",
    "files": ["two-recipes.jpg"],
    "recipes": [
      {"title": "Sauce", "servings": 2, "ingredients": ["10 g butter"]},
      {"title": "Salad", "servings": 2, "ingredients": ["100 g lettuce"]}
    ]
  },
  {
    "case": "handwritten",
    "files": ["note.jpg"],
    "recipes": [{"title": "Rice", "servings": 2, "ingredients": ["100 g rice"]}]
  }
]
```

Paths are relative to the manifest. Replace the illustrative recipe expectations
with the actual page contents. Expected ingredients are the complete verbatim
ingredient list in order. Run `uv run pytest -m live`; it skips clearly if keys or
the manifest are absent. This makes real paid API calls when configured. Caches
are written only to pytest temporary directories. After inspecting successful
results, copy the generated cache JSON into fixture files and replace their
archived page references with `@0`, `@1`, etc. before committing a recording.
