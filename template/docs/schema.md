# QA database schema notes

Only what tests need: how subjects are found, which tables and columns tests may change, and what makes a change
unsafe. The harness-setup skill fills this in; mirror "Allowed changes" into `[[allow]]` in `harness.toml`.

## Discovery

The queries `harness sql` uses to find a subject that matches a test's criteria, with their joins.

```sql
-- demo app; replace with the app's
SELECT p.PRODUCT_ID, p.NAME, p.BASE_PRICE, r.RULE_ID, r.PERCENT, r.ENABLED
FROM PRODUCT p LEFT JOIN DISCOUNT_RULE r ON r.PRODUCT_ID = p.PRODUCT_ID
```

## Allowed changes

| Table | Row key | Columns tests may set | Ops | Meaning |
|---|---|---|---|---|
| `DISCOUNT_RULE` | `RULE_ID` | `PRODUCT_ID`, `PERCENT`, `ENABLED`, `CREATED_ON` | update, insert, delete | a product's discount |

## Excluded, and why

Tables or ops a test might want but may not have, with the reason (a cascading foreign key, a trigger, an identity
key, an audit table). A revert cannot undo what a cascade or trigger did elsewhere.

| Table | Not allowed | Reason |
|---|---|---|
| `JOB_RESULT` | everything | the app writes it; tests read it through `collect` |

## Open questions

- Does the app write rows of its own during a run (results, audit)? They are not cleaned up; plan for them.
- Can subjects be cloned into scratch copies? If so, prefer changing clones over shared rows.
