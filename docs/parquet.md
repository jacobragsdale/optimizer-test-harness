# Optimizer output (parquet on NFS)

Fill this in at work. `harness results <manifest>` prints a summary of every file; `harness results <manifest> --sql`
runs duckdb SQL where each file is a view named by its stem (`holdings.parquet` becomes `holdings`).

| File | Grain | Key columns | Used to check |
|---|---|---|---|
| `holdings.parquet` | one row per security | `portfolio_id`, `security_id`, `sector`, `weight` | weights, sector sums, holding counts |
| `summary.parquet` | one row per run | `run_id`, `objective_value`, `tracking_error`, `turnover`, `holdings` | headline metrics |

The rows above describe the stub's output. Replace them with the real layout, including units (weights as fractions
or percent, turnover one-way or two-way) since evaluation plans depend on it.
