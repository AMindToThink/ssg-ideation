import copy
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from ssg_ideation import client as client_mod
from ssg_ideation.client import (
    CallError,
    build_request,
    call,
    classify,
    make_http_client,
    turn2_messages,
)
from ssg_ideation.config import (
    JUDGE_MODEL,
    POST_DRAFT,
    TARGET_MODELS,
    TURN1_PROMPT,
    TURN2_PROMPT,
    model_by_key,
)
from ssg_ideation.run import validate_model

FAKE_KEY = "sk-or-v1-THIS-IS-A-FAKE-SECRET-0123456789"
GEMINI = model_by_key("gemini-4-argon")


def completion_body(
    content: str = "1. An idea.",
    finish: str = "stop",
    native: str = "STOP",
    provider: str = "Google AI Studio",
) -> dict[str, Any]:
    return {
        "id": "gen-123",
        "provider": provider,
        "model": GEMINI.model_id,
        "choices": [
            {
                "finish_reason": finish,
                "native_finish_reason": native,
                "message": {
                    "role": "assistant",
                    "content": content,
                    "reasoning": "thinking...",
                    "reasoning_details": [
                        {"type": "reasoning.encrypted", "data": "abc", "id": "r1", "format": "google-gemini-v1"}
                    ],
                },
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 20},
    }


def generation_body(provider: str = "Google AI Studio", tier: str | None = None) -> dict[str, Any]:
    return {
        "data": {
            "id": "gen-123",
            "provider_name": provider,
            "service_tier": None,
            "total_cost": 0.01,
            "native_tokens_completion": 20,
            "native_tokens_reasoning": 5,
            "provider_responses": [
                {"provider_name": provider, "routed_service_tier": tier, "status": 200}
            ],
        }
    }


class FakeAPI:
    """Mock OpenRouter: records requests, serves a canned completion and generation."""

    def __init__(
        self,
        completion: tuple[int, dict[str, Any]] = (200, completion_body()),
        generation: dict[str, Any] | None = None,
        generation_404s: int = 0,
    ) -> None:
        self.completion = completion
        self.generation = generation or generation_body()
        self.generation_404s = generation_404s
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path.endswith("/chat/completions"):
            status, body = self.completion
            return httpx.Response(status, json=body)
        if request.url.path.endswith("/generation"):
            if self.generation_404s > 0:
                self.generation_404s -= 1
                return httpx.Response(404, json={"error": {"message": "not found"}})
            return httpx.Response(200, json=self.generation)
        raise AssertionError(f"unexpected request {request.url}")

    def client(self) -> httpx.Client:
        return make_http_client(httpx.MockTransport(self))


def do_call(api: FakeAPI, out: Path, expected: str = "Google AI Studio") -> dict[str, Any]:
    with api.client() as c:
        return call(
            c, FAKE_KEY, GEMINI, [{"role": "user", "content": TURN1_PROMPT}], out, expected,
            generation_meta_timeout_s=5.0,
        )


@pytest.fixture(autouse=True)
def fast_polls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client_mod.time, "sleep", lambda s: None)


# --- request builder -------------------------------------------------------


@pytest.mark.parametrize("spec", [*TARGET_MODELS, JUDGE_MODEL], ids=lambda s: s.key)
def test_request_has_exact_keys_and_no_system_message(spec: Any) -> None:
    body = build_request(spec, [{"role": "user", "content": "hi"}])
    assert set(body) == {"model", "messages", "provider", "reasoning", "max_tokens", "stream"}
    assert all(m["role"] != "system" for m in body["messages"])
    assert body["stream"] is False


@pytest.mark.parametrize("spec", TARGET_MODELS, ids=lambda s: s.key)
def test_provider_block_is_pinned(spec: Any) -> None:
    p = build_request(spec, [{"role": "user", "content": "hi"}])["provider"]
    assert p == {
        "order": [spec.provider],
        "only": [spec.provider],
        "allow_fallbacks": False,
        "require_parameters": True,
        "data_collection": "deny",
    }


def test_efforts_and_providers_per_model() -> None:
    got = {m.key: (m.model_id, m.provider, m.effort, m.max_tokens) for m in TARGET_MODELS}
    assert got == {
        "gpt-6-astra": ("openai/gpt-6-astra", "openai", "max", 128000),
        "claude-opus-5.5": ("anthropic/claude-opus-5.5", "anthropic", "max", 128000),
        "claude-fable-5.1": ("anthropic/claude-fable-5.1", "anthropic", "max", 128000),
        "gemini-4-argon": ("google/gemini-4-argon", "google-ai-studio", "high", 65536),
    }
    assert (JUDGE_MODEL.model_id, JUDGE_MODEL.provider) == ("deepseek/deepseek-v4.1-flash-1017", "deepseek")


def test_system_message_rejected() -> None:
    with pytest.raises(ValueError):
        build_request(GEMINI, [{"role": "system", "content": "x"}, {"role": "user", "content": "hi"}])


@pytest.mark.parametrize("bad", [{"temperature": 0.0}, {"top_p": 0.9}, {"seed": 1}, {"tools": []},
                                 {"provider": {}}])
def test_sampling_params_and_overrides_rejected(bad: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        build_request(GEMINI, [{"role": "user", "content": "hi"}], extra=bad)


def test_prompts_are_verbatim_from_post() -> None:
    text = POST_DRAFT.read_text()
    assert TURN1_PROMPT in text
    assert TURN2_PROMPT in text


# --- turn 2 assembly ---------------------------------------------------------


def test_turn2_echoes_assistant_message_verbatim() -> None:
    msg = completion_body()["choices"][0]["message"]
    original = copy.deepcopy(msg)
    messages = turn2_messages(TURN1_PROMPT, msg, TURN2_PROMPT)
    assert messages == [
        {"role": "user", "content": TURN1_PROMPT},
        original,
        {"role": "user", "content": TURN2_PROMPT},
    ]
    assert messages[1]["reasoning_details"] == original["reasoning_details"]
    assert msg == original  # not mutated


def test_turn2_requires_reasoning_details() -> None:
    msg = {"role": "assistant", "content": "x"}
    with pytest.raises(ValueError):
        turn2_messages(TURN1_PROMPT, msg, TURN2_PROMPT)


# --- classification ----------------------------------------------------------


def test_classify_outcomes() -> None:
    assert classify(200, completion_body()) == ("ok", None)
    assert classify(200, completion_body(finish="length"))[0] == "truncated"
    assert classify(200, completion_body(finish="stop", native="refusal"))[0] == "refused"
    assert classify(200, completion_body(finish="content_filter"))[0] == "refused"
    assert classify(200, completion_body(content=""))[0] == "error"
    assert classify(500, {"error": {"message": "boom"}})[0] == "error"
    assert classify(403, {"error": {"message": "x", "metadata": {"reasons": ["cyber"]}}})[0] == "refused"
    assert classify(403, {"error": {"message": "forbidden"}})[0] == "error"
    assert classify(200, {"error": {"message": "mid-response failure"}})[0] == "error"


# --- logging, secrets, resumability -------------------------------------------


def test_record_never_contains_api_key(tmp_path: Path) -> None:
    out = tmp_path / "gen1_turn1.json"
    api = FakeAPI()
    record = do_call(api, out)
    assert record["outcome"] == "ok" and record["provider_check"] == "passed"
    # The key was actually sent...
    assert api.requests[0].headers["authorization"] == f"Bearer {FAKE_KEY}"
    # ...but never written.
    assert FAKE_KEY not in out.read_text()
    assert record["request"]["headers"]["Authorization"] == "Bearer <redacted>"


def test_error_record_never_contains_api_key(tmp_path: Path) -> None:
    out = tmp_path / "gen1_turn1.json"
    api = FakeAPI(completion=(500, {"error": {"message": "boom"}}))
    with pytest.raises(CallError) as exc:
        do_call(api, out)
    assert not out.exists()
    err_files = list((tmp_path / "errors").glob("*.json"))
    assert len(err_files) == 1
    assert FAKE_KEY not in err_files[0].read_text()
    assert FAKE_KEY not in str(exc.value)
    assert json.loads(err_files[0].read_text())["response"]["status"] == 500


def test_record_has_full_io(tmp_path: Path) -> None:
    out = tmp_path / "gen1_turn1.json"
    record = do_call(FakeAPI(generation_404s=2), out)
    on_disk = json.loads(out.read_text())
    assert on_disk == record
    assert record["request"]["body"]["messages"][0]["content"] == TURN1_PROMPT
    assert record["response"]["body"]["choices"][0]["message"]["reasoning"] == "thinking..."
    assert record["generation"]["body"]["data"]["provider_name"] == "Google AI Studio"
    assert record["generation"]["n_404_polls"] == 2
    assert {"git_commit", "git_dirty", "python", "packages"} <= set(record["environment"])
    assert record["latency_s"] >= 0 and record["started_at"] <= record["ended_at"]


def test_complete_record_is_not_rerequested(tmp_path: Path) -> None:
    out = tmp_path / "gen1_turn1.json"
    do_call(FakeAPI(), out)
    api2 = FakeAPI()
    do_call(api2, out)
    assert api2.requests == []


def test_pending_record_only_fetches_generation(tmp_path: Path) -> None:
    out = tmp_path / "gen1_turn1.json"
    first = do_call(FakeAPI(), out)
    pending = {k: v for k, v in first.items() if k not in ("generation", "provider_problems")}
    pending["provider_check"] = "pending"
    out.write_text(json.dumps(pending))
    api2 = FakeAPI()
    record = do_call(api2, out)
    assert [r.url.path for r in api2.requests] == ["/api/v1/generation"]
    assert record["provider_check"] == "passed"


def test_wrong_provider_fails_loudly_and_stays_failed(tmp_path: Path) -> None:
    out = tmp_path / "gen1_turn1.json"
    api = FakeAPI(completion=(200, completion_body(provider="Google")),
                  generation=generation_body(provider="Google"))
    with pytest.raises(CallError, match="served-provider"):
        do_call(api, out)
    assert json.loads(out.read_text())["provider_check"] == "failed"
    api2 = FakeAPI()
    with pytest.raises(CallError, match="served-provider"):
        do_call(api2, out)
    assert api2.requests == []


def test_service_tier_routing_fails(tmp_path: Path) -> None:
    out = tmp_path / "gen1_turn1.json"
    with pytest.raises(CallError, match="routed_service_tier"):
        do_call(FakeAPI(generation=generation_body(tier="flex")), out)


def test_response_service_tier_fails(tmp_path: Path) -> None:
    body = completion_body()
    body["service_tier"] = "priority"
    with pytest.raises(CallError, match="response.service_tier"):
        do_call(FakeAPI(completion=(200, body)), tmp_path / "gen1_turn1.json")


def test_moderation_refusal_is_recorded_not_raised(tmp_path: Path) -> None:
    out = tmp_path / "gen1_turn1.json"
    body = {"error": {"code": 403, "message": "flagged", "metadata": {"reasons": ["cyber"]}}}
    api = FakeAPI(completion=(403, body))
    record = do_call(api, out)
    assert record["outcome"] == "refused"
    assert record["provider_check"] == "not_applicable"
    assert [r.url.path for r in api.requests] == ["/api/v1/chat/completions"]


# --- preflight model validation ----------------------------------------------


def _models_and_endpoints(effort: str = "high", max_out: int = 943718) -> tuple[dict[str, Any], dict[str, Any]]:
    models = {GEMINI.model_id: {"reasoning": {"supported_efforts": [effort, "low"]}}}
    endpoints = {
        "data": {
            "endpoints": [
                {"tag": "google-ai-studio", "provider_name": "Google AI Studio", "service_tier": None,
                 "supported_parameters": ["reasoning", "max_tokens"], "max_completion_tokens": max_out},
                {"tag": "google-ai-studio/flex", "provider_name": "Google AI Studio", "service_tier": "flex",
                 "supported_parameters": ["reasoning", "max_tokens"], "max_completion_tokens": max_out},
            ]
        }
    }
    return models, endpoints


def test_validate_model_ok() -> None:
    models, endpoints = _models_and_endpoints()
    assert validate_model(GEMINI, models, endpoints, {"reasoning", "max_tokens"}) == "Google AI Studio"


def test_validate_model_rejects_unsupported_effort() -> None:
    models, endpoints = _models_and_endpoints(effort="medium")
    with pytest.raises(RuntimeError, match="supported_efforts"):
        validate_model(GEMINI, models, endpoints, {"reasoning"})


def test_validate_model_rejects_max_tokens_over_cap() -> None:
    models, endpoints = _models_and_endpoints(max_out=1000)
    with pytest.raises(RuntimeError, match="max_completion_tokens"):
        validate_model(GEMINI, models, endpoints, {"reasoning"})


def test_validate_model_rejects_missing_param_and_model() -> None:
    models, endpoints = _models_and_endpoints()
    with pytest.raises(RuntimeError, match="unsupported parameters"):
        validate_model(GEMINI, models, endpoints, {"response_format"})
    with pytest.raises(RuntimeError, match="not listed"):
        validate_model(GEMINI, {}, endpoints, set())
