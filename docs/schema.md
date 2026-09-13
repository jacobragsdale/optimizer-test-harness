# QA database schema notes

Fill this in at work. Only what a test needs: how portfolios are identified and described, where constraints and
parameters live, and which columns tests are allowed to change (mirror the `[[allow]]` list in `harness.toml`).

## Portfolio discovery

Tables and columns `harness sql` should query to find a portfolio matching a test's criteria. Include the joins.

```sql
-- sample data shape (harness/stub.py); replace with the real query
SELECT p.PORTFOLIO_ID, p.NAME, p.MARKET_CAP_BUCKET, p.HOLDINGS_COUNT, c.CONSTRAINT_TYPE, c.LIMIT_VALUE, c.ENABLED
FROM PORTFOLIO p LEFT JOIN PORTFOLIO_CONSTRAINT c ON c.PORTFOLIO_ID = p.PORTFOLIO_ID
```

## Allowed changes

| Table | Row key | Columns tests may set | Meaning |
|---|---|---|---|
| `PORTFOLIO_CONSTRAINT` | `PORTFOLIO_ID`, `CONSTRAINT_TYPE` | `LIMIT_VALUE`, `ENABLED` | constraint limit and on/off flag |

## Open questions (answer before running against QA)

- Does the optimizer write anything back to the database (trade lists, results tables)? If so, those rows need
  cleanup too and the harness does not yet know about them.
- Can portfolios be cloned into a scratch copy? If yes, prefer mutating clones over shared rows.
- Is undo retention configured (Oracle)? `SELECT ... AS OF TIMESTAMP` then gives an independent revert check.
