# Optimizer REST API

Fill this in at work, then mirror the field names into the `[api]` section of `harness.toml`.

## Submit

`POST <submit_url>` with the JSON body from the spec's `run.params`, verbatim. Document the required fields here.

```json
{"portfolio_id": "PF-1002"}
```

Response must contain the run id (`run_id_field`).

## Status

`GET <status_url>` with `{run_id}` substituted. Response must contain the status (`status_field`) and, once finished,
the output location (`output_path_field`) as a path the machine running the harness can read.

| Status | Terminal | Success |
|---|---|---|
| queued, running | no | |
| done | yes | yes |
| failed, cancelled | yes | no |

## UI

`run_url` is the page BAs open for a run; the report puts it in the Runs and Results sheets as a hyperlink.

## Authentication

Bearer token from `HARNESS_API_TOKEN` if set. Change `_headers` in `harness/optimizer.py` if the API wants something else.
