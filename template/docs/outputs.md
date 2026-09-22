# Evidence: what a run leaves behind

`harness results <run>` summarizes every view; `harness results <run> --sql` runs duckdb SQL over them. Each file
is a view named by its stem. The harness-setup skill fills in the app's rows; evaluation plans depend on the grain
and units, so state them.

| View | Source | Grain | Key columns | Used to check |
|---|---|---|---|---|
| `get_prices` | response of `get_prices` (JSON) | one row; `prices` is an array | `prices[].product_id`, `final_price`, `discount_percent`, `markup_percent` | final prices |
| `wait_job` | last response of `wait_job` (JSON) | one row | `status`, `error`, `output_path` | rejections |
| `prices_export` | app output file `{output_path}/prices_export.csv` | one row per product | `product_id`, `final_price` | exported prices |
| `<collect name>` | `collect` query, snapshotted before the revert (CSV) | per query | per query | DB state during the run |

Arrays in a JSON response need `unnest`:
`SELECT p.product_id, p.final_price FROM (SELECT unnest(prices) AS p FROM get_prices)`.

Units in the demo: prices in currency units with two decimals; percents as whole numbers (30 means 30%). Replace
the rows above with the app's.
