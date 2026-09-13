"""Optimizer REST adapter: submit a run, poll its status to a terminal state."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from pydantic import BaseModel, Field, JsonValue, TypeAdapter

from harness import HarnessError
from harness.config import RECORD, STRICT, ApiConfig

Http = Callable[[str, str, bytes | None, dict[str, str]], bytes]
"""(method, url, body, headers) -> response body."""

_JSON = TypeAdapter[JsonValue](JsonValue)


class StatusEvent(BaseModel):
    model_config = STRICT
    at: datetime
    status: str


class RunRecord(BaseModel):
    """Everything about one optimizer run that BAs and auditors ask for."""

    model_config = RECORD
    run_id: str
    url: str
    submitted_at: datetime
    request: dict[str, JsonValue]
    submit_response: JsonValue
    status_history: list[StatusEvent] = Field(default_factory=list)
    final_status: str | None = None
    succeeded: bool | None = None
    output_path: str | None = None
    last_response: JsonValue = None


def urllib_http(method: str, url: str, body: bytes | None, headers: dict[str, str]) -> bytes:
    request = urllib.request.Request(url, data=body, method=method, headers=headers)
    with urllib.request.urlopen(request, timeout=60) as response:
        body: bytes = response.read()
        return body


def utc_now() -> datetime:
    return datetime.now(UTC)


def _headers(token: str | None) -> dict[str, str]:
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _call(http: Http, method: str, url: str, body: bytes | None, token: str | None) -> JsonValue:
    try:
        raw = http(method, url, body, _headers(token))
    except urllib.error.HTTPError as exc:
        with exc:
            detail = exc.read()[:500].decode(errors="replace")
        msg = f"{method} {url} failed: HTTP {exc.code} {detail}"
        raise HarnessError(msg) from exc
    except urllib.error.URLError as exc:
        msg = f"{method} {url} failed: {exc.reason}"
        raise HarnessError(msg) from exc
    try:
        return _JSON.validate_python(json.loads(raw))
    except ValueError as exc:
        msg = f"{method} {url} returned non-JSON: {raw[:200]!r}"
        raise HarnessError(msg) from exc


def _field(payload: JsonValue, name: str) -> JsonValue:
    if not isinstance(payload, dict) or name not in payload:
        msg = f"response has no field {name!r}; check the [api] field names in harness.toml. Response: {json.dumps(payload)[:300]}"
        raise HarnessError(msg)
    return payload[name]


def submit(api: ApiConfig, token: str | None, params: dict[str, JsonValue], *, http: Http = urllib_http, now: Callable[[], datetime] = utc_now) -> RunRecord:
    payload = _call(http, "POST", api.submit_url, json.dumps(params).encode(), token)
    run_id = str(_field(payload, api.run_id_field))
    return RunRecord(run_id=run_id, url=api.run_url.format(run_id=run_id), submitted_at=now(), request=params, submit_response=payload)


def poll(
    api: ApiConfig,
    token: str | None,
    record: RunRecord,
    *,
    http: Http = urllib_http,
    now: Callable[[], datetime] = utc_now,
    sleep: Callable[[float], None] = time.sleep,
    on_event: Callable[[RunRecord], None] | None = None,
) -> RunRecord:
    """Poll until a terminal status or the configured timeout. Mutates and returns `record`."""
    deadline = now() + timedelta(seconds=api.timeout_seconds)
    while True:
        payload = _call(http, "GET", api.status_url.format(run_id=record.run_id), None, token)
        status = str(_field(payload, api.status_field))
        record.last_response = payload
        if not record.status_history or record.status_history[-1].status != status:
            record.status_history = [*record.status_history, StatusEvent(at=now(), status=status)]
            if on_event is not None:
                on_event(record)
        if status in api.terminal_statuses:
            output = payload.get(api.output_path_field) if isinstance(payload, dict) else None
            record.final_status = status
            record.succeeded = status in api.success_statuses
            record.output_path = None if output is None else str(output)
            return record
        if now() >= deadline:
            msg = f"run {record.run_id} still {status!r} after {api.timeout_seconds:g}s; check {record.url}"
            raise HarnessError(msg)
        sleep(api.poll_seconds)
