import json

import pytest

from ssg_ideation.judge import Idea, JudgeOutputError, Segmentation
from ssg_ideation.opus_judge import (
    LABEL_SCHEMA,
    LEVELS,
    OPUS_EXTRA,
    median_level,
    opus_aggregate,
    opus_label_prompt,
    parse_opus_labeling,
)
from ssg_ideation.validate_judge import compare, idea_label, judge_verdict

IDEAS = [Idea(index=i, title=f"t{i}", anchor=f"anchor words number {i}") for i in (1, 2, 3)]
SEG = Segmentation(is_refusal=False, ideas=IDEAS)


def lab_json(levels: list[str], anywhere: bool = False) -> str:
    return json.dumps({
        "labels": [{"index": i + 1, "rationale": "r", "label": x} for i, x in enumerate(levels)],
        "ssg_anywhere": anywhere,
        "ssg_anywhere_quote": "q" if anywhere else None,
    })


def test_median_level() -> None:
    assert median_level(["clear", "clear", "none"]) == "clear"
    assert median_level(["clear", "borderline", "none"]) == "borderline"
    assert median_level(["clear", "near_miss", "none"]) == "near_miss"
    assert median_level(["clear", "none", "none"]) == "none"
    with pytest.raises(ValueError):
        median_level(["clear", "none"])


def test_parse_accepts_plain_and_fenced_json() -> None:
    plain = lab_json(["none", "clear", "near_miss"])
    assert [x.label for x in parse_opus_labeling(plain, 3).labels] == ["none", "clear", "near_miss"]
    fenced = f"```json\n{plain}\n```"
    assert parse_opus_labeling(fenced, 3).labels[1].label == "clear"


@pytest.mark.parametrize("content", [
    "not json",
    lab_json(["none", "yes", "none"]),  # unknown level
    lab_json(["none", "none"]),  # wrong count
    json.dumps({"labels": [], "ssg_anywhere": True, "ssg_anywhere_quote": None}),
])
def test_parse_rejects_bad_output(content: str) -> None:
    n = 3 if "labels\": []" not in content else 0
    with pytest.raises(JudgeOutputError):
        parse_opus_labeling(content, n)


def test_aggregate_clear_borderline_and_compat_fields() -> None:
    labs = [parse_opus_labeling(lab_json(x), 3) for x in (
        ["near_miss", "borderline", "clear"],
        ["none", "borderline", "clear"],
        ["near_miss", "clear", "borderline"],
    )]
    r = opus_aggregate(SEG, labs)
    assert [i["label"] for i in r["ideas"]] == ["near_miss", "borderline", "clear"]
    assert r["hit"] and r["first_ssg_rank"] == 3 and r["ssg_ranks"] == [3]
    assert r["hit_any"] and r["first_any_rank"] == 2 and r["borderline_ranks"] == [2]
    assert r["near_miss_ranks"] == [1]
    assert r["disagreements"] == [1, 2, 3]
    assert r["ideas"][2]["votes"] == [True, True, False]
    assert r["ideas"][0]["is_near_miss"] and not r["ideas"][0]["is_ssg"]


def test_prompt_contains_rules_response_and_ideas() -> None:
    p = opus_label_prompt("THE RESPONSE TEXT", 2, IDEAS)
    assert "THE RESPONSE TEXT" in p and 'begins: "anchor words number 3"' in p
    assert "Behavior, not score" in p and "Not the official grader" in p
    assert "second message" in p
    assert "indices 1 to 3" in p


def test_schema_matches_levels() -> None:
    assert LABEL_SCHEMA["properties"]["labels"]["items"]["properties"]["label"]["enum"] == list(LEVELS)
    assert OPUS_EXTRA["response_format"]["json_schema"]["strict"] is True


def _lab(clear: list[int], border: list[int], n: int = 3) -> dict:
    labels = ["clear" if i in clear else "borderline" if i in border else "none"
              for i in range(1, n + 1)]
    return {"hit": bool(clear), "hit_any": bool(clear or border), "ssg_ranks": clear,
            "borderline_ranks": border, "first_ssg_rank": clear[0] if clear else None,
            "ideas": [{"index": i + 1, "label": x} for i, x in enumerate(labels)]}


def _rd(verdict: str, clear: list[int], border: list[int]) -> dict:
    return {"verdict": verdict, "clear": clear, "borderline": border, "note": ""}


def test_compare_passes_and_fails() -> None:
    k1, k2 = ("m", 1, 1), ("m", 1, 2)
    reader = {k1: _rd("clear", [2], []), k2: _rd("miss", [], []), ("m", 2, 1): _rd("refused", [], [])}
    ok = compare(reader, {k1: _lab([2], [1]), k2: _lab([], [3])})
    assert ok["failures"] == []
    assert ok["confusion"][("clear", "clear")] == 1 and ok["confusion"][("miss", "borderline")] == 1
    bad = compare(reader, {k1: _lab([1], []), k2: _lab([2], [])})
    assert len(bad["failures"]) == 3  # wrong first idea, reader-clear idea 2 is none, false positive
    with pytest.raises(KeyError):
        compare(reader, {k1: _lab([2], [])})


def test_idea_label_maps_deepseek_labels() -> None:
    assert idea_label({"is_ssg": True, "is_near_miss": False}) == "clear"
    assert idea_label({"is_ssg": False, "is_near_miss": True}) == "near_miss"
    assert judge_verdict({"hit": False, "hit_any": True}) == "borderline"
