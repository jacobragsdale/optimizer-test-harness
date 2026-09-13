---
name: optimizer-test-run
description: "Run a compiled optimizer test spec: plan, apply, run, evaluate, revert, report. Use whenever the user wants to run, execute, rerun, or verify a test case or regression set, even if they don't name the harness."
---

# Optimizer test run

Run one compiled spec through the harness CLI and judge the result. The CLI owns every side effect: it applies the
changes, submits the run, polls, and reverts inside a `finally` block. This skill covers the plan review, the
evaluation, the verdict, and the report.

## Workflow

Every command is `uv run --env-file .env harness ...` from the repo root. Run it, read the output, decide.

1. `harness status`. When it reports a pending revert, stop: tell the user, then run
   `harness revert <manifest> --yes`. Nothing else runs while changes are sitting in QA.
2. `harness validate specs/<case>.yaml`.
3. `harness run-case specs/<case>.yaml` with no flag. It prints the plan with the current database values and
   changes nothing. Show the plan to the user and wait for them to say go.
4. `harness run-case specs/<case>.yaml --yes`, only after the user approved that plan. Never pass `--yes` on your
   own initiative and never use it to skip step 3. The command applies, submits, polls to a terminal status, reverts,
   and prints the manifest path, run id, UI url and output path. A nonzero exit means an error is recorded in the
   manifest; read the manifest before doing anything else.
5. `harness results <manifest.json>` for a summary of every parquet file, then
   `harness results <manifest.json> --sql "<duckdb SQL>"` once per step of `expected.evaluation_plan`. Each file is
   a view named by its stem. Query exactly what the plan said you would look at, then anything else you need to
   explain a surprise.
6. Record the verdict:
   `harness verdict <manifest.json> pass|fail|inconclusive --evidence "<observed value>" --evidence "..." --notes "<one sentence>"`.
   Evidence is quoted numbers or rows from step 5, one observation per flag.
7. After the last case: `harness report --out <results.xlsx> --workbook <ba-workbook.xlsx>`. Give the user the
   path and two lines per case: verdict, and the one piece of evidence that decided it.

## Judging

- Quote values. "max sector weight 0.0987, cap 0.10" is evidence; "weights looked fine" is not.
- `outcome: failure` cases pass when the run ended in a failure status and the reason matches the expectation.
  The reason is in the manifest under `run.last_response`.
- A failure status when success was expected is `fail`, unless the failure is about the harness or the network
  rather than the optimizer. Then it is `inconclusive` with the error in notes.
- `inconclusive` is the correct answer when the output lacks what the evaluation plan needs, or when the plan itself
  turned out to be wrong. Put the corrected plan in notes so the compile skill can fix the spec. Never stretch to a
  pass or a fail.
- The database is already reverted while you evaluate. Judge from the parquet and the manifest, not from QA.

## Validation

- After `run-case --yes`: the output ends with `reverted: True` and `harness status` shows no pending revert.
  Otherwise tell the user now; `harness revert <manifest> --yes` is the repair.
- After `verdict`: `harness status` lists the case with its verdict.
- After `report`: the file has `Results`, `Runs` and `Changes` sheets and the run urls are clickable.

## Example

User: "run TC-001".

```text
harness status                                  -> pending revert: none
harness validate specs/TC-001.yaml              -> OK
harness run-case specs/TC-001.yaml              -> PORTFOLIO_CONSTRAINT {...} . LIMIT_VALUE: 0.2 -> 0.1
  (show plan, user says go)
harness run-case specs/TC-001.yaml --yes        -> run_id 3f9c...  status: done  reverted: True
harness results runs/TC-001/<ts>.json --sql "SELECT sector, sum(weight) w FROM holdings GROUP BY 1 ORDER BY 2 DESC"
                                                -> TECH 0.0987 ...
harness verdict runs/TC-001/<ts>.json pass --evidence "max sector weight 0.0987 <= cap 0.10" --notes "cap binds; all five sectors present"
```

Reply to the user: "TC-001 pass. Max sector weight 0.0987 against a 0.10 cap. Run 3f9c..., changes reverted."

## Bundled resources

All paths are relative to the repo root.

- `docs/parquet.md`: read before writing evaluation SQL; it names the files and columns.
- `docs/api.md`: read when a run ends in an unexpected status.
- `harness --help` and `harness <command> --help`: run when unsure about a flag.
