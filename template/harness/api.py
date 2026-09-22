"""Web app adapter: call a named operation, capture values from its response, poll it to a terminal status.

A non-2xx response or a poll that ends outside `success` is a result (`ok=False`), recorded like any other; the app
said no. Only network trouble, a timeout, or a response the config cannot read is a HarnessError.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, JsonValue, TypeAdapter

from harness import HarnessError
from harness.config import RECORD, STRICT, VAR, ApiConfig, Operation

Http = Callable[[str, str, bytes | None, dict[str, str]], tuple[int, str, bytes]]
"""(method, url, body, headers) -> (status, content type, body). Non-2xx is returned, not raised."""
Phase = Literal["login", "setup", "run", "undo"]

_JSON = TypeAdapter[JsonValue](JsonValue)
_ENV = re.compile(r"\$\{(\w+)\}")
_EXTENSIONS = {"application/json": ".json", "text/csv": ".csv"}


class StatusEvent(BaseModel):
    model_config = STRICT
    at: datetime
    status: str


class StepRecord(BaseModel):
    """One operation call as it happened. `request_body` is the template after `{var}` rendering, before `${ENV}`."""

    model_config = RECORD
    phase: Phase
    op: str
    method: str
    url: str
    request_body: JsonValue = None
    started_at: datetime
    http_status: int | None = None
    response_file: str | None = None
    """Relative to the run folder. The last response for a polled operation."""
    captured: dict[str, JsonValue] = Field(default_factory=dict)
    status_history: list[StatusEvent] = Field(default_factory=list)
    final_status: str | None = None
    ok: bool | None = None
    detail: str = ""

    @property
    def outcome(self) -> str:
        """What BAs see: the final poll status, else the HTTP status."""
        return self.final_status or (f"HTTP {self.http_status}" if self.http_status is not None else "no response")


def utc_now() -> datetime:
    return datetime.now(UTC)


def urllib_session(timeout: float) -> Http:
    """An HTTP client that keeps cookies across calls, so a login operation's session carries through the case."""
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))

    def http(method: str, url: str, body: bytes | None, headers: dict[str, str]) -> tuple[int, str, bytes]:
        request = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            with opener.open(request, timeout=timeout) as response:
                content_type: str = response.headers.get_content_type()
                return int(response.status), content_type, bytes(response.read())
        except urllib.error.HTTPError as exc:
            with exc:
                error_type: str = exc.headers.get_content_type()
                return exc.code, error_type, exc.read()
        except (urllib.error.URLError, OSError) as exc:
            msg = f"{method} {url} failed: {getattr(exc, 'reason', exc)}"
            raise HarnessError(msg) from exc

    return http


def render(value: JsonValue, variables: dict[str, JsonValue]) -> JsonValue:
    """Substitute `{var}` in strings. A string that is exactly `"{var}"` takes the variable's own type."""
    if isinstance(value, str):
        whole = VAR.fullmatch(value)
        if whole:
            return _lookup(variables, whole.group(1))
        return VAR.sub(lambda m: str(_lookup(variables, m.group(1))), value)
    if isinstance(value, list):
        return [render(v, variables) for v in value]
    if isinstance(value, dict):
        return {k: render(v, variables) for k, v in value.items()}
    return value


def render_path(template: str, variables: dict[str, JsonValue]) -> str:
    return VAR.sub(lambda m: urllib.parse.quote(str(_lookup(variables, m.group(1))), safe=""), template)


def _lookup(variables: dict[str, JsonValue], name: str) -> JsonValue:
    if name not in variables:
        msg = f"{{{name}}} was never captured in this case"
        raise HarnessError(msg)
    return variables[name]


def expand_env(value: JsonValue) -> JsonValue:
    """Substitute `${ENV}` in strings. Applied to the wire copy only; manifests keep the template."""
    if isinstance(value, str):
        return _ENV.sub(lambda m: _env(m.group(1)), value)
    if isinstance(value, list):
        return [expand_env(v) for v in value]
    if isinstance(value, dict):
        return {k: expand_env(v) for k, v in value.items()}
    return value


def _env(name: str) -> str:
    if name not in os.environ:
        msg = f"environment variable {name} is not set (harness.toml refers to it as ${{{name}}}); add it to .env"
        raise HarnessError(msg)
    return os.environ[name]


def dot_path(payload: JsonValue, path: str) -> JsonValue:
    """`a.b.0.c` into nested JSON objects and arrays."""
    value = payload
    for part in path.split("."):
        if isinstance(value, dict) and part in value:
            value = value[part]
        elif isinstance(value, list) and part.isdigit() and int(part) < len(value):
            value = value[int(part)]
        else:
            msg = f"response has no {path!r} (stopped at {part!r}); check the operation's capture or poll field in harness.toml. Response: {json.dumps(payload)[:300]}"
            raise HarnessError(msg)
    return value


def call(
    api: ApiConfig,
    op: Operation,
    body: JsonValue,
    variables: dict[str, JsonValue],
    http: Http,
    folder: Path,
    phase: Phase,
    *,
    now: Callable[[], datetime] = utc_now,
    sleep: Callable[[float], None] = time.sleep,
    on_event: Callable[[StepRecord], None] | None = None,
) -> StepRecord:
    """Call `op` (and poll it, if configured). Saves the last response under `folder` and captures on success."""
    url = api.base_url.rstrip("/") + render_path(op.path, variables)
    request_body = render(op.body if body is None else body, variables)
    record = StepRecord(phase=phase, op=op.name, method=op.method, url=url, request_body=request_body, started_at=now())
    wire = None if request_body is None else json.dumps(expand_env(request_body)).encode()
    headers = {"Accept": "application/json", "Content-Type": "application/json"} | {k: str(expand_env(v)) for k, v in api.headers.items()}
    deadline = now() + timedelta(seconds=op.poll.timeout_seconds if op.poll else 0)
    while True:
        status, content_type, raw = http(op.method, url, wire, headers)
        record.http_status = status
        record.response_file = _save_response(folder, op.name, content_type, raw, record.response_file)
        if not 200 <= status < 300:
            record.ok = False
            record.detail = f"HTTP {status}: {raw[:300].decode(errors='replace')}"
            return record
        payload = _parse(raw, content_type, op, url)
        if op.poll is None:
            break
        state = str(dot_path(payload, op.poll.field))
        if not record.status_history or record.status_history[-1].status != state:
            record.status_history = [*record.status_history, StatusEvent(at=now(), status=state)]
            if on_event is not None:
                on_event(record)
        if state in op.poll.until:
            record.final_status = state
            if state not in op.poll.success:
                record.ok = False
                record.detail = f"ended {state!r}"
                return record
            break
        if now() >= deadline:
            msg = f"{op.name} still {state!r} after {op.poll.timeout_seconds:g}s at {url}"
            raise HarnessError(msg)
        sleep(op.poll.every_seconds)
    record.captured = {name: dot_path(payload, path) for name, path in op.capture.items()}
    record.ok = True
    return record


def _parse(raw: bytes, content_type: str, op: Operation, url: str) -> JsonValue:
    if not raw.strip():
        return None
    if content_type != "application/json" and not (op.capture or op.poll):
        return None
    try:
        return _JSON.validate_python(json.loads(raw))
    except ValueError as exc:
        msg = f"{op.method} {url} returned non-JSON ({content_type}) but {op.name} captures or polls: {raw[:200]!r}"
        raise HarnessError(msg) from exc


def _save_response(folder: Path, op_name: str, content_type: str, raw: bytes, previous: str | None) -> str | None:
    """`<op>.json` (or .csv/.txt), `<op>_2.json` for the second call of the same operation. Polls overwrite their own file."""
    if not raw.strip():
        return previous
    folder.mkdir(parents=True, exist_ok=True)
    name = previous
    if name is None:
        ext = _EXTENSIONS.get(content_type, ".txt")
        name, n = f"{op_name}{ext}", 2
        while (folder / name).exists():
            name, n = f"{op_name}_{n}{ext}", n + 1
    (folder / name).write_bytes(raw)
    return name
