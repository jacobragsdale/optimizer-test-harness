---
name: test-run
description: "Run a compiled test spec: plan, set up, run, evaluate, revert, report. Use whenever the user wants to run, execute, rerun, or verify a test case or regression set, even if they don't name the harness."
---

# Test run

Run compiled specs through the harness CLI, one case at a time, and judge each result. The CLI owns every side
effect: it logs in, applies the setup, calls the app, snapshots the `collect` queries, and reverts inside a `finally`
block. This skill covers the plan review, the evaluation, the verdict, and the report.

## Asking

Ask when the answer changes which cases run, whether a case reruns, or how a set continues after a harness error.
Otherwise take the default the specs, `harness status` or an earlier answer gives, and say which one you took. Never
ask the user to settle a verdict; the evidence decides it (see Judging).

- Collect the open questions first, then ask up to four per round. Give a choice two to four concrete options,
  your recommendation first with its reason; ask for a plain fact (a URL, a path) plainly.
- Wait for the answers; never go on with an answer you assumed.

## Workflow

Every command is `uv run --env-file .env harness ...` from the repo root. Run it, read the output, decide.

1. `harness status`. When it reports a pending revert, nothing else runs until it is cleared:
   run `harness revert <run>` with no flag, show the user what it will undo, and run it with `--yes` only after
   they say go. When it reports an interrupted API call, tell the user exactly which call to check in the app; they
   clean it up and delete `runs/PENDING_REVERT` themselves.
2. Settle what runs. For a set ("the regression set", "all pricing cases"), choose the specs, in order, and show
   the list with the plans in step 4. Ask first when nothing in the repo defines the set or the request has more
   than one plausible reading (a sheet or a feature, the tagged cases or all). Flag each case that `harness status`
   already shows with a verdict.
3. `harness validate` on every spec in the list.
4. `harness run-case specs/<case>.yaml` with no flag, for every spec in the list. It prints the plan with the current
   database values and the full URLs, and changes nothing. Show all the plans together and wait for the user to say
   go; one go covers the list and the plans as shown. A spec that changes after the go needs its plan shown again.
5. `harness run-case specs/<case>.yaml --yes`, one case at a time in list order, only for plans the user approved.
   Never pass `--yes` on your own initiative and never use it to skip step 4. The command prints the run folder,
   the outcome, `run_ok` and the link. A nonzero exit means an error is recorded in `<run>/manifest.json`; read it,
   stop the set, and ask whether to retry the case, skip it, or stop. Output without `reverted: True` stops the set
   for good: go back to step 1.
6. `harness results <run>` for a summary of every view, then `harness results <run> --sql "<duckdb SQL>"` once per
   step of `expected.evaluation_plan`. Views: each response by its operation name, each collect by its name, each
   app output file by its stem. Query exactly what the plan said you would look at, then anything else you need to
   explain a surprise.
7. Record the verdict:
   `harness verdict <run> pass|fail|inconclusive --evidence "<observed value>" --evidence "..." --notes "<one sentence>"`.
   Evidence is quoted numbers or rows from step 6, one observation per flag.
8. After the last case: `harness report --out <results.xlsx> --workbook <ba-workbook.xlsx>`. Use the workbook the
   specs were compiled from; ask for it when it is not in the repo. Pass `--out results.xlsx` (repo root) unless the
   user named another path; ask before overwriting an existing file. Give the user the path and two lines per
   case: the verdict, and the one piece of evidence that decided it.

## Judging

- Quote values. "P-200 final_price 175.0, expected 175.00" is evidence; "prices looked right" is not.
- `outcome: failure` cases pass when the run ended rejected (`run_ok: False`) for the expected reason. The reason
  is in the rejected step's response view (for example `SELECT status, error FROM wait_job`) and its `detail` in
  the manifest.
- `run_ok: False` when success was expected is `fail`. When instead the manifest has `errors` (network, timeout,
  config), the harness failed, not the app: `inconclusive` with the error in notes.
- `inconclusive` is the correct answer when the evidence lacks what the evaluation plan needs, or when the plan
  itself turned out to be wrong. Put the corrected plan in notes so the compile skill can fix the spec. Never
  stretch to a pass or a fail.
- The database is already reverted while you evaluate. Judge from the run folder and the output files, not from
  QA. When the verdict needed database state from during the run, the spec lacked a `collect`: say so in notes.

## Validation

- After `run-case --yes`: the output ends with `reverted: True` and `harness status` shows no pending revert.
  Otherwise tell the user now; step 1 is the repair.
- After `verdict`: `harness status` lists the case with its verdict.
- After `report`: the file has `Results`, `Runs` and `Changes` sheets and the links are clickable.

## Example

User: "run TC-001".

```text
harness status                                  -> pending revert: none
harness validate specs/TC-001.yaml              -> OK
harness run-case specs/TC-001.yaml              -> db update DISCOUNT_RULE {'RULE_ID': 'R-2'}: PERCENT 15.0 -> 30 ...
  (show plan, user says go)
harness run-case specs/TC-001.yaml --yes        -> run: runs/TC-001/<ts>  outcome: done  run_ok: True  reverted: True
harness results runs/TC-001/<ts> --sql "SELECT p.product_id, p.final_price FROM (SELECT unnest(prices) AS p FROM get_prices)"
                                                -> P-200 | 175.0
harness verdict runs/TC-001/<ts> pass --evidence "P-200 final_price 175.0, expected 175.00" --notes "30% discount applied"
```

Reply to the user: "TC-001 pass. The chair priced at 175.00 as expected. Changes reverted."

## Bundled resources

All paths are relative to the repo root.

- `docs/outputs.md`: read before writing evaluation SQL; it names the views and columns.
- `docs/api.md`: read when a run ends in an unexpected status.
- `harness --help` and `harness <command> --help`: run when unsure about a flag.
