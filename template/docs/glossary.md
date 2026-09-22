# Glossary: BA vocabulary to data

Each entry maps a phrase BAs use in test cases to the table and column, API operation, or output column it means.
The harness-setup skill fills this in for the app; the test-compile skill reads it before interpreting a workbook
and adds entries it had to guess.

| BA says | Means | Where |
|---|---|---|
| discount on a product | `DISCOUNT_RULE.PERCENT` for rows with that `PRODUCT_ID` and `ENABLED = 1`, summed | QA database |
| remove / turn off a discount | delete the `DISCOUNT_RULE` row, or set `ENABLED = 0` | QA database |
| price list, markup | `create_price_list` operation, `markup_percent` in its body | API |
| reprice, run pricing | `submit_job` then `wait_job` (then `get_prices`) | API |
| comes out at, final price | `final_price` in the `get_prices` response, or `prices_export.csv` | API response / output file |
| rejected as invalid | `wait_job` ends in status `failed` with an `error` | API response |

The rows above describe the demo app (`harness/stub.py`). Replace them with the app's.
