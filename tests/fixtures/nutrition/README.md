# Nutrition acceptance fixtures

`test_nutrition.py` builds synthetic USDA-shaped responses, including a 30-name
ranking table. They exercise calculation, ranking, cache and retry behavior;
their small IDs are test values, not real USDA food IDs or captured API responses.
`test_cronometer.py` likewise uses synthetic CSV layouts. A real account export
and three manual paste imports remain necessary for external acceptance.
`test_portions.py` uses synthetic USDA-shaped portion rows, not real onion
weights. During live acceptance, compare the food description, portion count and
gram weight with USDA, including one size-based count, a multi-item measure, an
FNDDS row without an explicit amount and a cup/chopped row that must not be
accepted as a whole-item count. Check the selected recipe-weight basis against
the ingredient's confirmed edible fraction.

To run the deferred five-food USDA check, set `FDC_API_KEY` and
`BOURDAIN_LIVE_FDC` to a JSON manifest path, then run:

```sh
uv run pytest -m live tests/test_live.py -k five_live_fdc_foods
```

The manifest is a list of exactly five objects, each containing a verified
`fdc_id`, exact `description`, and expected `protein_g` per 100 g. Choose the
records and expected values from USDA before running the check. All fetched
data is cached under pytest's temporary directory; this does not touch a real
kitchen. Preserve reviewed responses as explicitly attributed offline fixtures
before closing P5.1/P5.2. Never put an API key in a manifest or fixture.
