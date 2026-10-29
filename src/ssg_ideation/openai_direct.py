"""OpenAI-API conversations for the timeline models (Responses API, store=false).

Each call writes one JSON record, like client.call: complete calls go to
data/raw/<key>/gen<g>_turn<t>.json and are never re-requested; failed attempts
go to errors/ and the run stops. Records keep the raw API request and response
and add a chat-style `message` and a `stats` block (finish reasons, tokens, and a
cost computed from config prices, as the OpenAI API returns none), so the judge
and analysis read them like OpenRouter records.

Follow-up turns send the previous request's input, then the previous response's
output items verbatim (reasoning items with their encrypted content included),
then the next prompt: the Responses-API form of echoing reasoning_details.

Usage:
    uv run python -m ssg_ideation.openai_direct --preflight-only
    uv run python -m ssg_ideation.openai_direct --models gpt-5.1 --gens 1 --turns 1   # smoke test
    uv run python -m ssg_ideation.openai_direct
"""

import argparse
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from ssg_ideation.client import (
    CallError,
    environment_info,
    error_path,
    is_complete,
    load_api_key,
    redact_headers,
    request_headers,
    utc_now,
    write_json_atomic,
)
from ssg_ideation.config import (
    N_GENERATIONS,
    N_TURNS,
    OPENAI_BASE,
    RAW_DIR,
    SNAPSHOT_DIR,
    TIMELINE_DIRECT,
    TURN_PROMPTS,
    DirectModel,
)
from ssg_ideation.run import check_env_ignored, log, raw_path, refused_turn

EFFORT_LADDER: tuple[str, ...] = ("minimal", "low", "medium", "high", "xhigh", "max")
# Stop the run if one call costs more than this (USD).
MAX_CALL_COST_USD: float = 25.0


class QuotaError(CallError):
    """The OpenAI key is out of credit (HTTP 429 insufficient_quota)."""


def model_by_key(key: str) -> DirectModel:
    for m in TIMELINE_DIRECT:
        if m.key == key:
            return m
    raise KeyError(f"unknown OpenAI-API model {key!r}")


def build_body(model: DirectModel, items: list[dict[str, Any]], max_output_tokens: int | None = None) -> dict[str, Any]:
    """Responses-API body. No instructions (system prompt), no sampling params, no tools."""
    body: dict[str, Any] = {
        "model": model.model_id,
        "input": items,
        "store": False,
        "max_output_tokens": max_output_tokens or model.max_output_tokens,
    }
    if model.effort is not None:
        body["reasoning"] = {"effort": model.effort, "summary": "auto"}
        body["include"] = ["reasoning.encrypted_content"]
    return body


def followup_items(prev_record: dict[str, Any], prompt: str) -> list[dict[str, Any]]:
    """The previous request's input, its output items verbatim, then the next prompt."""
    output = prev_record["response"]["body"]["output"]
    if prev_record["request"]["body"].get("reasoning") and not any(
        o.get("type") == "reasoning" and o.get("encrypted_content") for o in output
    ):
        raise ValueError("previous response has no encrypted reasoning item to send back")
    return [*prev_record["request"]["body"]["input"], *output, {"role": "user", "content": prompt}]


def normalize(body: dict[str, Any]) -> dict[str, Any]:
    """Chat-style assistant message from a Responses-API body."""
    texts: list[str] = []
    refusals: list[str] = []
    summaries: list[str] = []
    for item in body.get("output") or []:
        if item.get("type") == "message":
            for part in item.get("content") or []:
                if part.get("type") == "output_text":
                    texts.append(part.get("text", ""))
                elif part.get("type") == "refusal":
                    refusals.append(part.get("refusal", ""))
        elif item.get("type") == "reasoning":
            summaries += [s.get("text", "") for s in item.get("summary") or []]
    return {
        "role": "assistant",
        "content": "".join(texts) or None,
        "refusal": "\n".join(refusals) or None,
        "reasoning": "\n\n".join(summaries) or None,
    }


def stats(body: dict[str, Any], model: DirectModel) -> dict[str, Any]:
    usage = body.get("usage") or {}
    prompt = usage.get("input_tokens")
    completion = usage.get("output_tokens")
    cost = None
    if prompt is not None and completion is not None:
        # Cached input is billed at full price here, so this is an upper bound.
        cost = (prompt * model.price_in + completion * model.price_out) / 1e6
    return {
        "finish_reason": body.get("status"),
        "native_finish_reason": (body.get("incomplete_details") or {}).get("reason"),
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "reasoning_tokens": (usage.get("output_tokens_details") or {}).get("reasoning_tokens"),
        "cost_usd": cost,
        "served_model": body.get("model"),
    }


def classify(status: int, body: Any) -> tuple[str, str | None]:
    """(outcome, reason): ok, truncated, refused, quota or error."""
    if not isinstance(body, dict):
        return "error", "response body is not a JSON object"
    if status == 429 and (body.get("error") or {}).get("code") == "insufficient_quota":
        return "quota", "insufficient_quota"
    if status != 200:
        return "error", f"HTTP {status}: {(body.get('error') or {}).get('message')}"
    msg = normalize(body)
    reason = (body.get("incomplete_details") or {}).get("reason")
    if msg["refusal"] or reason == "content_filter":
        return "refused", f"refusal={bool(msg['refusal'])} incomplete_reason={reason}"
    if not (msg["content"] or "").strip():
        return "error", f"empty content (status={body.get('status')}, incomplete_reason={reason})"
    if body.get("status") == "incomplete" and reason == "max_output_tokens":
        return "truncated", "max_output_tokens"
    if body.get("status") != "completed":
        return "error", f"unexpected status={body.get('status')} reason={reason}"
    return "ok", None


def call(
    client: httpx.Client, api_key: str, model: DirectModel, items: list[dict[str, Any]], out_path: Path,
    record_extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Make (or resume) one logged call and return its record."""
    body = build_body(model, items)
    if out_path.exists():
        record = json.loads(out_path.read_text())
        if record["request"]["body"] != body:
            raise CallError("stored request differs from the one built now; move the record "
                            "aside before re-requesting", out_path)
        if not is_complete(record):
            raise CallError(f"unexpected record state {record.get('outcome')!r}", out_path)
        return record
    headers = request_headers(api_key)
    record: dict[str, Any] = {
        "model_key": model.key,
        **(record_extra or {}),
        "route": "openai-responses",
        "api_key_env": model.api_key_env,
        "environment": environment_info(),
        "request": {"url": f"{OPENAI_BASE}/responses", "headers": redact_headers(headers), "body": body},
        "started_at": utc_now(),
    }
    t0 = time.monotonic()
    try:
        r = client.post("/responses", json=body, headers=headers,
                        timeout=httpx.Timeout(connect=30.0, read=model.read_timeout_s, write=60.0, pool=60.0))
    except httpx.HTTPError as e:
        record |= {"ended_at": utc_now(), "latency_s": time.monotonic() - t0,
                   "outcome": "error", "reason": f"{type(e).__name__}: {e}"}
        path = error_path(out_path)
        write_json_atomic(path, record)
        raise CallError(f"{model.key}: transport error {type(e).__name__}", path) from e
    record["ended_at"] = utc_now()
    record["latency_s"] = time.monotonic() - t0
    try:
        resp_body: Any = json.loads(r.text)
    except json.JSONDecodeError:
        resp_body = None
    record["response"] = {"status": r.status_code, "headers": dict(r.headers), "body": resp_body,
                          **({"text": r.text} if resp_body is None else {})}
    outcome, reason = classify(r.status_code, resp_body)
    record["outcome"], record["reason"] = outcome, reason
    if outcome in ("error", "quota"):
        path = error_path(out_path)
        write_json_atomic(path, record)
        raise (QuotaError if outcome == "quota" else CallError)(f"{model.key}: {reason}", path)
    record["message"] = normalize(resp_body)
    record["stats"] = stats(resp_body, model)
    # The OpenAI API is the provider; there is no router to check.
    record["provider_check"] = "not_applicable"
    record["provider_problems"] = []
    write_json_atomic(out_path, record)
    return record


def check_effort(client: httpx.Client, api_key: str, model: DirectModel, out_dir: Path) -> None:
    """Fail unless the API accepts model.effort and rejects the next effort up.

    Two tiny calls ("Say hi.", max_output_tokens=16); both are saved under out_dir.
    """
    items = [{"role": "user", "content": "Say hi."}]
    probes: list[tuple[str, str | None]] = [("planned", model.effort)]
    if model.effort is not None and model.effort != EFFORT_LADDER[-1]:
        probes.append(("higher", EFFORT_LADDER[EFFORT_LADDER.index(model.effort) + 1]))
    for tag, effort in probes:
        body = build_body(DirectModel(**{**model.__dict__, "effort": effort}), items, max_output_tokens=16)
        r = client.post("/responses", json=body, headers=request_headers(api_key), timeout=600)
        write_json_atomic(out_dir / f"{model.key}_effort_{tag}.json", {
            "at": utc_now(), "request": {"url": f"{OPENAI_BASE}/responses",
                                         "headers": redact_headers(request_headers(api_key)), "body": body},
            "response": {"status": r.status_code, "body": r.json()},
        })
        if tag == "planned" and r.status_code != 200:
            raise RuntimeError(f"{model.key}: planned effort {effort!r} rejected: {r.text[:300]}")
        if tag == "higher" and not (r.status_code == 400 and "Unsupported value" in r.text):
            raise RuntimeError(f"{model.key}: effort {effort!r} above the planned one was not rejected "
                               f"(HTTP {r.status_code}); the planned effort is not the highest")


def preflight(client: httpx.Client, api_key: str, models: list[DirectModel]) -> None:
    check_env_ignored()
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    snap = SNAPSHOT_DIR / f"{stamp}_openai"
    listed = client.get("/models", headers=request_headers(api_key))
    listed.raise_for_status()
    write_json_atomic(snap / "openai_models.json", listed.json())
    ids = {m["id"] for m in listed.json()["data"]}
    for m in models:
        if m.model_id not in ids:
            raise RuntimeError(f"{m.model_id} is not listed in the OpenAI /models response")
        check_effort(client, api_key, m, snap)
    log(f"preflight ok: {len(models)} models listed, efforts confirmed; snapshot {snap}")


def run_conversation(
    client: httpx.Client, api_key: str, model: DirectModel, gen: int, turns: list[int], abort: threading.Event,
) -> None:
    for turn in turns:
        if abort.is_set():
            log(f"SKIP {model.key} gen{gen} turn{turn}: run aborted")
            return
        if turn == 1:
            items = [{"role": "user", "content": TURN_PROMPTS[1]}]
        else:
            refused = refused_turn(model, gen, turn)
            if refused is not None:
                log(f"SKIP {model.key} gen{gen} turn{turn}: turn {refused} was refused")
                return
            items = followup_items(json.loads(raw_path(model, gen, turn - 1).read_text()), TURN_PROMPTS[turn])
        out = raw_path(model, gen, turn)
        log(f"{'RESUME' if out.exists() else 'START'} {model.key} gen{gen} turn{turn}")
        try:
            record = call(client, api_key, model, items, out, {"generation_index": gen, "turn": turn})
        except Exception as e:
            abort.set()
            log(f"FAILED {model.key} gen{gen} turn{turn}: {e}")
            raise
        s = record["stats"]
        log(f"DONE {model.key} gen{gen} turn{turn}: outcome={record['outcome']} served={s['served_model']} "
            f"latency={record['latency_s']:.0f}s cost=${s['cost_usd']:.4f} out={s['completion_tokens']} "
            f"reasoning={s['reasoning_tokens']}")
        if (s["cost_usd"] or 0) > MAX_CALL_COST_USD:
            abort.set()
            raise RuntimeError(f"{model.key} gen{gen} turn{turn} cost ${s['cost_usd']:.2f} > ${MAX_CALL_COST_USD}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", nargs="+", default=[m.key for m in TIMELINE_DIRECT])
    ap.add_argument("--gens", nargs="+", type=int, default=list(range(1, N_GENERATIONS + 1)))
    ap.add_argument("--turns", nargs="+", type=int, default=list(range(1, N_TURNS + 1)))
    ap.add_argument("--preflight-only", action="store_true")
    ap.add_argument("--skip-preflight", action="store_true", help="efforts were confirmed by an earlier preflight")
    args = ap.parse_args(argv)
    models = [model_by_key(k) for k in args.models]
    turns = sorted(args.turns)
    if turns != list(range(turns[0], turns[-1] + 1)) or not 1 <= turns[0] <= turns[-1] <= N_TURNS:
        raise SystemExit(f"bad --turns {args.turns}")
    api_key = load_api_key(models[0].api_key_env)
    with httpx.Client(base_url=OPENAI_BASE, timeout=httpx.Timeout(60.0, connect=30.0)) as client:
        if not args.skip_preflight:
            preflight(client, api_key, models)
        if args.preflight_only:
            return 0
        abort = threading.Event()
        jobs = [(m, g) for m in models for g in args.gens]
        log(f"running {len(jobs)} conversations, turns {turns}")
        with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
            futures = {pool.submit(run_conversation, client, api_key, m, g, turns, abort): (m.key, g)
                       for m, g in jobs}
        errors = [(futures[f], f.exception()) for f in futures if f.exception() is not None]
    quota = [e for _, e in errors if isinstance(e, QuotaError)]
    for (key, gen), e in errors:
        log(f"ERROR {key} gen{gen}: {e}")
    if quota:
        log("OPENAI KEY OUT OF CREDIT: rerun the remaining models through OpenRouter on "
            "OPENROUTER_API_KEY_PERSONAL (see README)")
    if errors:
        log(f"RUN FAILED: {len(errors)} conversation(s) failed")
        return 1
    log("RUN COMPLETE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
