"""OpenRouter calls with full request/response logging and served-provider checks.

Every call writes one JSON record. Successful (or refused/truncated) calls go to
the caller's `out_path` and are never re-requested; failed attempts go to an
`errors/` directory next to it and the call raises.
"""

import functools
import importlib.metadata
import json
import os
import platform
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from dotenv import dotenv_values

from ssg_ideation.config import (
    API_KEY_ENV,
    FORBIDDEN_SERVICE_TIERS,
    GENERATION_META_TIMEOUT_S,
    OPENROUTER_BASE,
    READ_TIMEOUT_S,
    REPO_ROOT,
    ModelSpec,
)

# Outcomes that end a call. Anything else raises.
COMPLETE_OUTCOMES: frozenset[str] = frozenset({"ok", "truncated", "refused"})
SAMPLING_PARAMS: frozenset[str] = frozenset(
    {"temperature", "top_p", "top_k", "min_p", "frequency_penalty", "presence_penalty",
     "repetition_penalty", "seed"}
)


class CallError(RuntimeError):
    """A call failed. Its full record is on disk at `record_path`."""

    def __init__(self, message: str, record_path: Path) -> None:
        super().__init__(f"{message} (record: {record_path})")
        self.record_path = record_path


def load_api_key() -> str:
    key = os.environ.get(API_KEY_ENV) or dotenv_values(REPO_ROOT / ".env").get(API_KEY_ENV)
    if not key:
        raise RuntimeError(f"{API_KEY_ENV} is not set in the environment or {REPO_ROOT / '.env'}")
    return key


def make_http_client(transport: httpx.BaseTransport | None = None) -> httpx.Client:
    timeout = httpx.Timeout(connect=30.0, read=READ_TIMEOUT_S, write=60.0, pool=60.0)
    return httpx.Client(base_url=OPENROUTER_BASE, timeout=timeout, transport=transport)


def provider_block(spec: ModelSpec) -> dict[str, Any]:
    return {
        "order": [spec.provider],
        "only": [spec.provider],
        "allow_fallbacks": False,
        "require_parameters": True,
        "data_collection": "deny",
    }


def build_request(
    spec: ModelSpec, messages: list[dict[str, Any]], extra: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Chat-completions body. No system message, no sampling params, no tools."""
    if any(m.get("role") == "system" for m in messages):
        raise ValueError("system messages are not allowed")
    body: dict[str, Any] = {
        "model": spec.model_id,
        "messages": messages,
        "provider": provider_block(spec),
        "reasoning": {"effort": spec.effort},
        "max_tokens": spec.max_tokens,
        "stream": False,
    }
    if extra:
        clash = (set(extra) & set(body)) | (set(extra) & SAMPLING_PARAMS) | ({"tools"} & set(extra))
        if clash:
            raise ValueError(f"extra request fields not allowed: {sorted(clash)}")
        body.update(extra)
    return body


def request_headers(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}


def redact_headers(headers: dict[str, str]) -> dict[str, str]:
    return {k: ("Bearer <redacted>" if k.lower() == "authorization" else v) for k, v in headers.items()}


@functools.cache
def environment_info() -> dict[str, Any]:
    def git(*args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, check=True
        ).stdout.strip()

    return {
        "git_commit": git("rev-parse", "HEAD"),
        # Code paths only: data/ and results/ are written during runs.
        "git_dirty": bool(git("status", "--porcelain", "--", "src", "tests", "pyproject.toml", "uv.lock")),
        "python": platform.python_version(),
        "packages": {
            p: importlib.metadata.version(p) for p in ("httpx", "pydantic", "ssg-ideation")
        },
    }


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def write_json_atomic(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False))
    os.replace(tmp, path)


def error_path(out_path: Path) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    return out_path.parent / "errors" / f"{out_path.stem}_{stamp}.json"


def assistant_message(record: dict[str, Any]) -> dict[str, Any]:
    """The assistant message exactly as returned by the API."""
    return record["response"]["body"]["choices"][0]["message"]


def turn2_messages(
    turn1_prompt: str, turn1_assistant: dict[str, Any], turn2_prompt: str
) -> list[dict[str, Any]]:
    """Turn-1 exchange (assistant message echoed verbatim, incl. reasoning_details) + turn 2."""
    if "reasoning_details" not in turn1_assistant:
        raise ValueError("turn-1 assistant message has no reasoning_details to echo")
    return [
        {"role": "user", "content": turn1_prompt},
        turn1_assistant,
        {"role": "user", "content": turn2_prompt},
    ]


def classify(status: int, body: Any) -> tuple[str, str | None]:
    """(outcome, reason). outcome is one of COMPLETE_OUTCOMES or "error"."""
    if not isinstance(body, dict):
        return "error", "response body is not a JSON object"
    if status == 403 and isinstance(body.get("error"), dict):
        meta = body["error"].get("metadata") or {}
        if "reasons" in meta or "flagged_input" in meta:
            return "refused", "openrouter_moderation_403"
    if status != 200:
        return "error", f"HTTP {status}"
    if "error" in body:
        return "error", "error object in 200 response"
    choices = body.get("choices") or []
    if len(choices) != 1:
        return "error", f"expected 1 choice, got {len(choices)}"
    choice = choices[0]
    finish = choice.get("finish_reason")
    native = choice.get("native_finish_reason")
    if finish == "content_filter" or native == "refusal":
        return "refused", f"finish_reason={finish} native_finish_reason={native}"
    content = choice.get("message", {}).get("content")
    if not isinstance(content, str) or not content.strip():
        return "error", f"empty content (finish_reason={finish})"
    if finish == "length":
        return "truncated", "finish_reason=length"
    if finish != "stop":
        return "error", f"unexpected finish_reason={finish} native={native}"
    return "ok", None


def fetch_generation_meta(
    client: httpx.Client, api_key: str, generation_id: str, timeout_s: float, poll_s: float = 2.0
) -> dict[str, Any]:
    """Poll /generation until it is available. It lags the completion briefly."""
    deadline = time.monotonic() + timeout_s
    n_404 = 0
    while True:
        r = client.get("/generation", params={"id": generation_id}, headers=request_headers(api_key))
        if r.status_code == 200:
            return {"n_404_polls": n_404, "fetched_at": utc_now(), "body": r.json()}
        if r.status_code != 404 or time.monotonic() > deadline:
            raise RuntimeError(
                f"/generation?id={generation_id} returned {r.status_code} after {n_404} 404 polls: "
                f"{r.text[:500]}"
            )
        n_404 += 1
        time.sleep(poll_s)


def check_served_provider(
    body: dict[str, Any], meta: dict[str, Any], expected_provider_name: str
) -> list[str]:
    """Problems with who served the call; empty means the pinned endpoint served it."""
    problems: list[str] = []
    data = meta["body"]["data"]
    if body.get("provider") != expected_provider_name:
        problems.append(f"response.provider={body.get('provider')!r}")
    if data.get("provider_name") != expected_provider_name:
        problems.append(f"generation.provider_name={data.get('provider_name')!r}")
    if body.get("service_tier") in FORBIDDEN_SERVICE_TIERS:
        problems.append(f"response.service_tier={body.get('service_tier')!r}")
    if data.get("service_tier") in FORBIDDEN_SERVICE_TIERS:
        problems.append(f"generation.service_tier={data.get('service_tier')!r}")
    for pr in data.get("provider_responses") or []:
        if pr.get("provider_name") != expected_provider_name:
            problems.append(f"provider_responses has {pr.get('provider_name')!r}")
        if pr.get("routed_service_tier") is not None:
            problems.append(f"provider_responses routed_service_tier={pr.get('routed_service_tier')!r}")
    return problems


def is_complete(record: dict[str, Any]) -> bool:
    return record.get("outcome") in COMPLETE_OUTCOMES and record.get("provider_check") in {
        "passed", "not_applicable"
    }


def call(
    client: httpx.Client,
    api_key: str,
    spec: ModelSpec,
    messages: list[dict[str, Any]],
    out_path: Path,
    expected_provider_name: str,
    extra: dict[str, Any] | None = None,
    record_extra: dict[str, Any] | None = None,
    generation_meta_timeout_s: float = GENERATION_META_TIMEOUT_S,
) -> dict[str, Any]:
    """Make (or resume) one logged call and return its record.

    A complete record on disk is returned without a request. A record whose
    response is saved but whose /generation metadata is missing gets only the
    metadata fetch, so a crash never re-spends tokens.
    """
    if out_path.exists():
        record = json.loads(out_path.read_text())
        if is_complete(record):
            return record
        if record.get("provider_check") == "failed":
            raise CallError(f"served-provider check failed earlier: {record['provider_problems']}", out_path)
        if record.get("outcome") not in COMPLETE_OUTCOMES:
            raise CallError(f"unexpected record state {record.get('outcome')!r}", out_path)
        return _finish(client, api_key, record, out_path, expected_provider_name, generation_meta_timeout_s)

    body = build_request(spec, messages, extra)
    headers = request_headers(api_key)
    record: dict[str, Any] = {
        "model_key": spec.key,
        **(record_extra or {}),
        "environment": environment_info(),
        "expected_provider_name": expected_provider_name,
        "request": {
            "url": f"{OPENROUTER_BASE}/chat/completions",
            "headers": redact_headers(headers),
            "body": body,
        },
    }
    record["started_at"] = utc_now()
    t0 = time.monotonic()
    try:
        r = client.post("/chat/completions", json=body, headers=headers)
    except httpx.HTTPError as e:
        record |= {"ended_at": utc_now(), "latency_s": time.monotonic() - t0,
                   "outcome": "error", "reason": f"{type(e).__name__}: {e}"}
        path = error_path(out_path)
        write_json_atomic(path, record)
        raise CallError(f"{spec.key}: transport error {type(e).__name__}", path) from e
    record["ended_at"] = utc_now()
    record["latency_s"] = time.monotonic() - t0
    try:
        resp_body: Any = json.loads(r.text)
    except json.JSONDecodeError:
        resp_body = None
    record["response"] = {
        "status": r.status_code,
        "headers": dict(r.headers),
        "body": resp_body,
        **({"text": r.text} if resp_body is None else {}),
    }
    outcome, reason = classify(r.status_code, resp_body)
    record["outcome"] = outcome
    record["reason"] = reason
    if outcome == "error":
        path = error_path(out_path)
        write_json_atomic(path, record)
        raise CallError(f"{spec.key}: {reason}", path)
    record["provider_check"] = "pending"
    write_json_atomic(out_path, record)
    return _finish(client, api_key, record, out_path, expected_provider_name, generation_meta_timeout_s)


def _finish(
    client: httpx.Client,
    api_key: str,
    record: dict[str, Any],
    out_path: Path,
    expected_provider_name: str,
    generation_meta_timeout_s: float,
) -> dict[str, Any]:
    body = record["response"]["body"]
    if record["reason"] == "openrouter_moderation_403":
        # Rejected before any provider ran, so there is no generation to look up.
        record["generation"] = None
        record["provider_check"] = "not_applicable"
        record["provider_problems"] = []
        write_json_atomic(out_path, record)
        return record
    record["generation"] = fetch_generation_meta(
        client, api_key, body["id"], generation_meta_timeout_s
    )
    problems = check_served_provider(body, record["generation"], expected_provider_name)
    record["provider_problems"] = problems
    record["provider_check"] = "failed" if problems else "passed"
    write_json_atomic(out_path, record)
    if problems:
        raise CallError(f"served-provider check failed: {problems}", out_path)
    return record


def get_json(client: httpx.Client, path: str, api_key: str | None = None) -> Any:
    headers = request_headers(api_key) if api_key else {}
    r = client.get(path, headers=headers)
    r.raise_for_status()
    return r.json()
