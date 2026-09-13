# Glossary: BA vocabulary to data

Fill this in at work. Each entry maps a phrase BAs use in test cases to the tables, columns, request fields or
output columns it means. The compile skill reads this before interpreting a workbook, and adds entries it had to guess.

| BA says | Means | Where |
|---|---|---|
| sector cap, sector max, sector limit | `PORTFOLIO_CONSTRAINT.LIMIT_VALUE` where `CONSTRAINT_TYPE = 'SECTOR_MAX'` | QA database |
| turn the constraint off, disable | `PORTFOLIO_CONSTRAINT.ENABLED = 0` | QA database |
| mid cap portfolio | `PORTFOLIO.MARKET_CAP_BUCKET = 'MID'` | QA database |
| rerun, run the optimizer | POST to the submit endpoint with `portfolio_id` | optimizer API |
| optimized holdings | `holdings.parquet` in the run's output folder | NFS output |
| tracking error | `summary.parquet` column `tracking_error` | NFS output |

The rows above describe the local sample data in `harness/stub.py`. Replace them with the real ones.
