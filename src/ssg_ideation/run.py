"""Preflight checks, then the target conversations (each turn threaded on the last).

Usage:
    uv run python -m ssg_ideation.run                     # all models, gens 1-3, turns 1-3
    uv run python -m ssg_ideation.run --models gemini-4-argon --gens 1 --turns 1   # smoke test
"""

import argparse
import json
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from ssg_ideation.client import (
    call,
    followup_messages,
    get_json,
    load_api_key,
    make_http_client,
    utc_now,
    write_json_atomic,
)
from ssg_ideation.config import (
    JUDGE_MODEL,
    N_GENERATIONS,
    N_TURNS,
    RAW_DIR,
    REPO_ROOT,
    SNAPSHOT_DIR,
    TARGET_MODELS,
    TURN_PROMPTS,
    ModelSpec,
    model_by_key,
)

_print_lock = threading.Lock()


def log(msg: str) -> None:
    with _print_lock:
        print(f"[{utc_now()}] {msg}", flush=True)


def raw_path(spec: ModelSpec, gen: int, turn: int) -> Path:
    return RAW_DIR / spec.key / f"gen{gen}_turn{turn}.json"


def refused_turn(spec: ModelSpec, gen: int, turn: int) -> int | None:
    """The earlier turn of this conversation that was refused, if any.

    No turn is sent after a refusal, so later records never exist.
    """
    for t in range(1, turn):
        p = raw_path(spec, gen, t)
        if p.exists() and json.loads(p.read_text())["outcome"] == "refused":
            return t
    return None


def check_env_ignored() -> None:
    ignored = subprocess.run(["git", "check-ignore", "-q", ".env"], cwd=REPO_ROOT).returncode == 0
    tracked = (
        subprocess.run(
            ["git", "ls-files", "--error-unmatch", ".env"], cwd=REPO_ROOT, capture_output=True
        ).returncode
        == 0
    )
    if not ignored or tracked:
        raise RuntimeError(f".env must be git-ignored and untracked (ignored={ignored}, tracked={tracked})")


def validate_model(
    spec: ModelSpec, models: dict[str, dict[str, Any]], endpoints: dict[str, Any], required_params: set[str]
) -> str:
    """Raise if `spec` can't be served as configured; return the pinned endpoint's provider_name."""
    if spec.model_id not in models:
        raise RuntimeError(f"{spec.model_id} is not listed in /models")
    efforts = (models[spec.model_id].get("reasoning") or {}).get("supported_efforts") or []
    if spec.effort is not None and spec.effort not in efforts:
        raise RuntimeError(f"{spec.model_id}: effort {spec.effort!r} not in supported_efforts {efforts}")
    matches = [e for e in endpoints["data"]["endpoints"] if e["tag"] == spec.provider]
    if len(matches) != 1:
        tags = [e["tag"] for e in endpoints["data"]["endpoints"]]
        raise RuntimeError(f"{spec.model_id}: expected one endpoint tagged {spec.provider!r}, found {tags}")
    ep = matches[0]
    if ep.get("service_tier") is not None:
        raise RuntimeError(f"{spec.model_id}: pinned endpoint is a service tier ({ep['service_tier']})")
    missing = required_params - set(ep["supported_parameters"])
    if missing:
        raise RuntimeError(f"{spec.model_id}@{spec.provider}: unsupported parameters {sorted(missing)}")
    if spec.max_tokens > ep["max_completion_tokens"]:
        raise RuntimeError(
            f"{spec.model_id}@{spec.provider}: max_tokens {spec.max_tokens} > "
            f"endpoint max_completion_tokens {ep['max_completion_tokens']}"
        )
    return ep["provider_name"]


def preflight(
    client: httpx.Client, api_keys: dict[str, str], specs: list[ModelSpec], judge: bool = False
) -> dict[str, str]:
    """Fail-fast checks before any spend. Saves a snapshot of /models and endpoints.

    `api_keys` maps .env variable name -> key. Returns model key -> expected served
    provider_name.
    """
    check_env_ignored()
    missing = {s.api_key_env for s in specs} - set(api_keys)
    if missing:
        raise RuntimeError(f"no key loaded for {sorted(missing)}")
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    snap = SNAPSHOT_DIR / stamp
    remaining: dict[str, Any] = {}
    for env_name, api_key in api_keys.items():
        key_info = get_json(client, "/key", api_key)["data"]
        # Only spend-related fields: the key's label is a partial key.
        write_json_atomic(
            snap / f"key_limits_{env_name}.json",
            {k: key_info.get(k) for k in ("limit", "limit_remaining", "usage", "is_free_tier")},
        )
        remaining[env_name] = key_info.get("limit_remaining")
    models_json = get_json(client, "/models")
    write_json_atomic(snap / "models.json", models_json)
    models = {m["id"]: m for m in models_json["data"]}
    expected: dict[str, str] = {}
    for spec in specs:
        required = ({spec.max_tokens_param} | ({"reasoning"} if spec.effort is not None else set())
                    | ({"response_format"} if judge else set()))
        if spec.model_id not in models:
            raise RuntimeError(f"{spec.model_id} is not listed in /models")
        endpoints = get_json(client, f"/models/{spec.model_id}/endpoints")
        write_json_atomic(snap / f"{spec.key}_endpoints.json", endpoints)
        expected[spec.key] = validate_model(spec, models, endpoints, required)
    log(f"preflight ok; snapshot {snap.relative_to(REPO_ROOT)}; "
        f"key limit_remaining={remaining}; expected providers {expected}")
    return expected


def run_conversation(
    client: httpx.Client,
    api_key: str,
    spec: ModelSpec,
    gen: int,
    turns: list[int],
    expected_provider: str,
    abort: threading.Event,
) -> None:
    for turn in turns:
        if abort.is_set():
            log(f"SKIP {spec.key} gen{gen} turn{turn}: run aborted")
            return
        out = raw_path(spec, gen, turn)
        if turn == 1:
            messages = [{"role": "user", "content": TURN_PROMPTS[1]}]
        else:
            refused = refused_turn(spec, gen, turn)
            if refused is not None:
                log(f"SKIP {spec.key} gen{gen} turn{turn}: turn {refused} was refused")
                return
            prev = json.loads(raw_path(spec, gen, turn - 1).read_text())
            messages = followup_messages(prev, TURN_PROMPTS[turn], require_reasoning=spec.effort is not None)
        resumed = out.exists()
        log(f"{'RESUME' if resumed else 'START'} {spec.key} gen{gen} turn{turn} key={spec.api_key_env}")
        try:
            record = call(
                client, api_key, spec, messages, out, expected_provider,
                record_extra={"generation_index": gen, "turn": turn, "api_key_env": spec.api_key_env},
            )
        except Exception as e:
            abort.set()
            log(f"FAILED {spec.key} gen{gen} turn{turn}: {e}")
            raise
        gen_data = (record.get("generation") or {}).get("body", {}).get("data", {})
        log(
            f"DONE {spec.key} gen{gen} turn{turn}: outcome={record['outcome']} "
            f"latency={record.get('latency_s', 0):.0f}s cost=${gen_data.get('total_cost')} "
            f"tokens_completion={gen_data.get('native_tokens_completion')} "
            f"reasoning={gen_data.get('native_tokens_reasoning')}"
        )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", nargs="+", default=[m.key for m in TARGET_MODELS])
    ap.add_argument("--gens", nargs="+", type=int, default=list(range(1, N_GENERATIONS + 1)))
    ap.add_argument("--turns", nargs="+", type=int, default=list(range(1, N_TURNS + 1)))
    args = ap.parse_args(argv)
    specs = [model_by_key(k) for k in args.models]
    if JUDGE_MODEL in specs:
        raise SystemExit("the judge is not a target model")
    turns = sorted(args.turns)
    if turns != list(range(turns[0], turns[-1] + 1)) or not 1 <= turns[0] <= turns[-1] <= N_TURNS:
        raise SystemExit(f"bad --turns {args.turns}: need consecutive turns within 1..{N_TURNS}")

    api_keys = {env: load_api_key(env) for env in sorted({s.api_key_env for s in specs})}
    with make_http_client() as client:
        expected = preflight(client, api_keys, specs)
        abort = threading.Event()
        jobs = [(s, g) for s in specs for g in args.gens]
        log(f"running {len(jobs)} conversations, turns {turns}")
        with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
            futures = {
                pool.submit(
                    run_conversation, client, api_keys[s.api_key_env], s, g, turns, expected[s.key], abort
                ): (s.key, g)
                for s, g in jobs
            }
        errors = [(futures[f], f.exception()) for f in futures if f.exception() is not None]
    if errors:
        for (key, gen), e in errors:
            log(f"ERROR {key} gen{gen}: {e}")
        log(f"RUN FAILED: {len(errors)} conversation(s) failed")
        return 1
    log("RUN COMPLETE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
