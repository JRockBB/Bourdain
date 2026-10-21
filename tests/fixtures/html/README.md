# Reduced recipe metadata fixtures

Captured on 2026-10-21 from the URLs in the adjacent `.url` files. Each HTML file
retains only the recipe's name, yield, ingredient list, and timing metadata.
Instructions, photographs, article prose, and advertising were omitted. Tests
read these files offline; they never fetch the source pages.

The sources intentionally exercise different metadata shapes: BBC Good Food has
`Makes 12`, Budget Bytes has a list containing a numeric yield and prices within
ingredient lines, and Love and Lemons has a contradictory numeric/ranged yield.
Only the unambiguous numeric serving count is accepted automatically.
