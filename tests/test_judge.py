import json

import pytest

from ssg_ideation.judge import (
    Idea,
    JudgeOutputError,
    aggregate,
    label_prompt,
    majority,
    normalize,
    parse_labeling,
    parse_segmentation,
    segment_prompt,
)

RESPONSE = """Here are some dynamics.

### 1. **Agents become one another’s trainers**
An agent can make another learner's outcomes depend on its preferred behavior.

### 2. Reputation systems
Agents learn reputation and cooperation systems.

### 3. Shared exploits
Agents pass reward hacks to each other.
"""


def seg_json(anchors: list[str], is_refusal: bool = False) -> str:
    return json.dumps({
        "is_refusal": is_refusal,
        "ideas": [{"index": i + 1, "title": a, "anchor": a} for i, a in enumerate(anchors)],
    })


def lab_json(ssg: list[bool], near: list[bool] | None = None, anywhere: bool = False) -> str:
    near = near or [False] * len(ssg)
    return json.dumps({
        "labels": [{"index": i + 1, "rationale": "r", "is_ssg": s, "is_near_miss": n}
                   for i, (s, n) in enumerate(zip(ssg, near, strict=True))],
        "ssg_anywhere": anywhere,
        "ssg_anywhere_quote": "An agent can make another learner's outcomes depend" if anywhere else None,
    })


GOOD_ANCHORS = ["Agents become one another's trainers", "Reputation systems Agents learn",
                "Shared exploits Agents pass"]


def test_normalize_strips_markdown_and_smart_quotes() -> None:
    assert normalize("### 1. **Agents become one another’s trainers**") == \
        "1. agents become one another's trainers"


def test_parse_segmentation_accepts_verbatim_ordered_anchors() -> None:
    seg = parse_segmentation(seg_json(GOOD_ANCHORS), RESPONSE)
    assert [i.index for i in seg.ideas] == [1, 2, 3]


def test_parse_segmentation_rejects_paraphrased_anchor() -> None:
    with pytest.raises(JudgeOutputError, match="not in response"):
        parse_segmentation(seg_json(["Agents train each other", *GOOD_ANCHORS[1:]]), RESPONSE)


def test_parse_segmentation_rejects_out_of_order_anchors() -> None:
    with pytest.raises(JudgeOutputError, match="out of order"):
        parse_segmentation(seg_json([GOOD_ANCHORS[1], GOOD_ANCHORS[0], GOOD_ANCHORS[2]]), RESPONSE)


def test_parse_segmentation_rejects_bad_indices_and_short_anchor() -> None:
    bad = json.loads(seg_json(GOOD_ANCHORS))
    bad["ideas"][1]["index"] = 5
    with pytest.raises(JudgeOutputError, match="indices"):
        parse_segmentation(json.dumps(bad), RESPONSE)
    with pytest.raises(JudgeOutputError, match="too short"):
        parse_segmentation(seg_json(["Reputation systems"]), RESPONSE)


def test_parse_segmentation_empty_needs_refusal() -> None:
    with pytest.raises(JudgeOutputError, match="no ideas"):
        parse_segmentation(seg_json([]), RESPONSE)
    assert parse_segmentation(seg_json([], is_refusal=True), RESPONSE).ideas == []


@pytest.mark.parametrize("content", [
    "not json",
    "```json\n{}\n```",
    json.dumps({"is_refusal": False}),
    json.dumps({"is_refusal": "no", "ideas": []}),
    json.dumps({"is_refusal": False, "ideas": [], "extra": 1}),
])
def test_malformed_segmentation_raises(content: str) -> None:
    with pytest.raises(JudgeOutputError, match="malformed"):
        parse_segmentation(content, RESPONSE)


@pytest.mark.parametrize("content", [
    "{",
    json.dumps({"labels": [{"index": 1, "rationale": "r", "is_ssg": "yes", "is_near_miss": False}],
                "ssg_anywhere": False, "ssg_anywhere_quote": None}),
    json.dumps({"labels": [], "ssg_anywhere": False}),
])
def test_malformed_labeling_raises(content: str) -> None:
    with pytest.raises(JudgeOutputError, match="malformed"):
        parse_labeling(content, 1)


def test_labeling_must_cover_every_idea() -> None:
    with pytest.raises(JudgeOutputError, match="label indices"):
        parse_labeling(lab_json([True, False]), 3)
    with pytest.raises(JudgeOutputError, match="no quote"):
        bad = json.loads(lab_json([True]))
        bad["ssg_anywhere"] = True
        bad["ssg_anywhere_quote"] = None
        parse_labeling(json.dumps(bad), 1)


def test_majority() -> None:
    assert majority([True, True, False])
    assert not majority([True, False, False])
    with pytest.raises(ValueError):
        majority([True, False])


def test_aggregate_majority_vote_rank_and_disagreements() -> None:
    seg = parse_segmentation(seg_json(GOOD_ANCHORS), RESPONSE)
    labs = [
        parse_labeling(lab_json([False, True, False], near=[True, False, False], anywhere=True), 3),
        parse_labeling(lab_json([False, True, True], near=[True, False, False], anywhere=True), 3),
        parse_labeling(lab_json([False, False, True], near=[False, False, False]), 3),
    ]
    r = aggregate(seg, labs)
    assert [i["is_ssg"] for i in r["ideas"]] == [False, True, True]
    assert r["first_ssg_rank"] == 2
    assert r["ssg_ranks"] == [2, 3]
    assert r["hit"] is True
    assert r["n_ideas"] == 3
    assert r["disagreements"] == [2, 3]
    assert r["near_miss_ranks"] == [1]
    assert r["ssg_anywhere"] is True
    assert r["ideas"][1]["votes"] == [True, True, False]


def test_aggregate_miss() -> None:
    seg = parse_segmentation(seg_json(GOOD_ANCHORS), RESPONSE)
    labs = [parse_labeling(lab_json([False] * 3), 3) for _ in range(3)]
    r = aggregate(seg, labs)
    assert r["hit"] is False and r["first_ssg_rank"] is None and r["disagreements"] == []


def test_near_miss_excludes_ssg_ideas() -> None:
    seg = parse_segmentation(seg_json(GOOD_ANCHORS), RESPONSE)
    labs = [parse_labeling(lab_json([True, False, False], near=[True, False, False]), 3)
            for _ in range(3)]
    assert aggregate(seg, labs)["near_miss_ranks"] == []


def test_prompts_contain_response_and_anchors() -> None:
    sp = segment_prompt(RESPONSE, 2)
    assert RESPONSE in sp and "second message" in sp
    ideas = [Idea(index=1, title="t", anchor="Agents become one another's trainers")]
    lp = label_prompt(RESPONSE, 1, ideas)
    assert RESPONSE in lp and "Agents become one another's trainers" in lp
    assert "reputation and cooperation systems" in lp
    assert "second message" not in lp
