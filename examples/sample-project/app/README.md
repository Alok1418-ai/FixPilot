# `app` package

Three intentional defects live in this package, one per module:

| Module | Defect | Report |
| --- | --- | --- |
| `inventory.py` | unknown SKUs raise `KeyError` instead of reporting zero stock | `reports/bug-001-critical-stock-lookup.txt` |
| `pricing.py` | `average_price([])` divides by zero | `reports/bug-002-average-price-empty.txt` |
| `notes.py` | a bare `except` turns malformed quantities into `None` | `reports/bug-003-quantity-parsing.txt` |

Each one is covered by a failing test in `tests/`, so FixPilot can prove a fix
rather than assert one.
