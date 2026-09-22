# App API

The operations `harness.toml` defines, in words. The harness-setup skill fills this in; specs refer to operations by
name only.

## Authentication

Demo: the `login` operation posts a user and `${HARNESS_APP_PASSWORD}` to `/session`, and the session cookie it sets
is kept for the rest of the case. For token auth, use `[api] headers` with `${HARNESS_API_TOKEN}` instead.

## Operations

| Operation | Call | Used as | Captures | Undo |
|---|---|---|---|---|
| `login` | `POST /session` | login | | |
| `create_price_list` | `POST /price-lists` | setup | `price_list_id` | `delete_price_list` |
| `delete_price_list` | `DELETE /price-lists/{price_list_id}` | undo | | |
| `submit_job` | `POST /jobs` with `product_ids`, optional `price_list_id` | run | `job_id` | |
| `wait_job` | `GET /jobs/{job_id}`, polled | run | `output_path` | |
| `get_prices` | `GET /jobs/{job_id}/prices` | run | | |

## Statuses

| Operation | Status field | Values | Terminal | Success |
|---|---|---|---|---|
| `wait_job` | `status` | queued, running | no | |
| | | done | yes | yes |
| | | failed, cancelled | yes | no |

Rejections: `POST /jobs` answers 404 for an unknown price list and 422 for a bad body; a job whose discounts total
over 100% ends `failed` with an `error`.

## Links

`[report] link` is the page BAs open for a run; the report puts it in the Results sheet as a hyperlink.
