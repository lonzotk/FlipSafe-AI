# Local FlipSafe fixtures

Comparison records, seller notes, and messages here are synthetic and labeled
for local testing. Sample photos are public raster demo images, not seller
evidence.

- `full_data.json` is the comparison set used by the app.
- `test_data.json` contains exactly 30 standalone electronics comparison records
  retained as a local fixture; the app does not switch to it at runtime.
- `scam_test_cases.json` contains eight normal/scam buyer-message cases.
- `playbook_rules.md` contains reusable condition, listing, and buyer-safety rules.
- `sample_images/` contains six public raster demo photos for the supported device categories. Their local seller notes and comparisons remain synthetic; sources and licenses are recorded in `sample_images/SOURCES.md`.

The app always uses the full synthetic fixture set. It is not live market data.
