import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from ssg_ideation import openai_direct as od
from ssg_ideation.client import CallError, record_message, record_stats
from ssg_ideation.config import TIMELINE, TIMELINE_DIRECT, TIMELINE_OPENROUTER, TURN1_PROMPT, TURN2_PROMPT, model_by_key
from ssg_ideation.judge import judge_content

FAKE_KEY = "sk-proj-THIS-IS-A-FAKE-SECRET-0123456789"
GPT5 = od.model_by_key("gpt-5")


def response_body(text: str = "1. An idea.", status: str = "completed", reason: str | None = None,
                  refusal: str | None = None) -> dict[str, Any]:
    content: list[dict[str, Any]] = [{"type": "output_text", "text": text}] if text else []
    if refusal:
        content.append({"type": "refusal", "refusal": refusal})
    return {
        "id": "resp_1", "model": "gpt-5-2025-08-07", "status": status,
        "incomplete_details": {"reason": reason} if reason else None,
        "output": [
            {"id": "rs_1", "type": "reasoning", "encrypted_content": "enc", "summary": [{"type": "summary_text", "text": "thinking"}]},
            {"id": "msg_1", "type": "message", "role": "assistant", "content": content},
        ],
        "usage": {"input_tokens": 1000, "output_tokens": 2000, "output_tokens_details": {"reasoning_tokens": 1500}},
    }


def mock_client(status: int, body: dict[str, Any], seen: list[httpx.Request]) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, json=body)
    return httpx.Client(base_url="https://api.openai.com/v1", transport=httpx.MockTransport(handler))


def test_body_has_no_system_prompt_sampling_or_storage() -> None:
    body = od.build_body(GPT5, [{"role": "user", "content": TURN1_PROMPT}])
    assert set(body) == {"model", "input", "store", "max_output_tokens", "reasoning", "include"}
    assert body["store"] is False and body["reasoning"] == {"effort": "high", "summary": "auto"}
    assert body["include"] == ["reasoning.encrypted_content"]


def test_followup_sends_back_output_items_verbatim() -> None:
    prev = {"request": {"body": od.build_body(GPT5, [{"role": "user", "content": TURN1_PROMPT}])},
            "response": {"body": response_body()}}
    items = od.followup_items(prev, TURN2_PROMPT)
    assert items == [{"role": "user", "content": TURN1_PROMPT}, *response_body()["output"],
                     {"role": "user", "content": TURN2_PROMPT}]
    prev["response"]["body"]["output"][0]["encrypted_content"] = None
    with pytest.raises(ValueError):
        od.followup_items(prev, TURN2_PROMPT)


def test_classify() -> None:
    assert od.classify(200, response_body()) == ("ok", None)
    assert od.classify(200, response_body(status="incomplete", reason="max_output_tokens"))[0] == "truncated"
    assert od.classify(200, response_body(text="", refusal="no"))[0] == "refused"
    assert od.classify(200, response_body(text="", status="incomplete", reason="max_output_tokens"))[0] == "error"
    assert od.classify(429, {"error": {"code": "insufficient_quota"}})[0] == "quota"
    assert od.classify(400, {"error": {"message": "bad"}})[0] == "error"


def test_call_records_normalized_message_cost_and_no_key(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []
    out = tmp_path / "gpt-5" / "gen1_turn1.json"
    with mock_client(200, response_body(), seen) as c:
        rec = od.call(c, FAKE_KEY, GPT5, [{"role": "user", "content": TURN1_PROMPT}], out)
        assert FAKE_KEY not in out.read_text()
        assert record_message(rec)["content"] == "1. An idea." and record_message(rec)["reasoning"] == "thinking"
        assert judge_content(rec) == "1. An idea."
        assert record_stats(rec)["cost_usd"] == pytest.approx((1000 * 1.25 + 2000 * 10.0) / 1e6)
        od.call(c, FAKE_KEY, GPT5, [{"role": "user", "content": TURN1_PROMPT}], out)  # resume
    assert len(seen) == 1
    with mock_client(200, response_body(), seen) as c, pytest.raises(CallError):
        od.call(c, FAKE_KEY, GPT5, [{"role": "user", "content": "changed"}], out)


def test_quota_error_is_its_own_exception(tmp_path: Path) -> None:
    with mock_client(429, {"error": {"code": "insufficient_quota", "message": "no credit"}}, []) as c:
        with pytest.raises(od.QuotaError):
            od.call(c, FAKE_KEY, GPT5, [{"role": "user", "content": "x"}], tmp_path / "a.json")
    errors = list((tmp_path / "errors").glob("*.json"))
    assert len(errors) == 1 and FAKE_KEY not in errors[0].read_text()


def test_timeline_points_name_known_models_in_date_order() -> None:
    known = {m.key for m in (*TIMELINE_DIRECT, *TIMELINE_OPENROUTER)} | {"gpt-6-astra"}
    assert {p.key for p in TIMELINE} == known
    dates = [p.released for p in TIMELINE]
    assert dates == sorted(dates)
    assert all(model_by_key(p.key).label for p in TIMELINE)
    assert all(m.effort is None for m in TIMELINE_OPENROUTER)


def test_effort_check_requires_rejection_of_the_next_effort(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        effort = json.loads(request.content)["reasoning"]["effort"]
        if effort == "high":
            return httpx.Response(200, json=response_body())
        return httpx.Response(400, json={"error": {"message": "Unsupported value: 'xhigh'"}})
    with httpx.Client(base_url="https://api.openai.com/v1", transport=httpx.MockTransport(handler)) as c:
        od.check_effort(c, FAKE_KEY, GPT5, tmp_path)
    accept_all = httpx.MockTransport(lambda r: httpx.Response(200, json=response_body()))
    with httpx.Client(base_url="https://api.openai.com/v1", transport=accept_all) as c, pytest.raises(RuntimeError):
        od.check_effort(c, FAKE_KEY, GPT5, tmp_path)
