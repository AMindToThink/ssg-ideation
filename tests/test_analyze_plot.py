from pathlib import Path
from typing import Any

import pytest

from ssg_ideation.analyze import build_rows, hit_counts, render_report, write_summary
from ssg_ideation.plot import make_figure, read_summary

MODELS = ["gemini-4-argon", "claude-opus-5.5"]


def record(outcome: str = "ok", content: str = "1. idea") -> dict[str, Any]:
    msg: dict[str, Any] = {"role": "assistant", "content": content if outcome != "refused" else None,
                           "reasoning": "thoughts", "refusal": "blocked" if outcome == "refused" else None}
    finish = {"ok": "stop", "truncated": "length", "refused": "content_filter"}[outcome]
    return {
        "outcome": outcome,
        "reason": None if outcome == "ok" else "r",
        "latency_s": 12.345,
        "response": {"body": {"choices": [{"finish_reason": finish, "native_finish_reason": finish,
                                           "message": msg}]}},
        "generation": {"body": {"data": {"native_tokens_prompt": 10, "native_tokens_completion": 20,
                                         "native_tokens_reasoning": 5, "total_cost": 0.5}}},
    }


def label(hit: bool, rank: int | None = None, n: int = 5) -> dict[str, Any]:
    ideas = [{"index": i, "title": f"idea {i}", "anchor": "a b c", "votes": [i == rank] * 3,
              "is_ssg": i == rank, "near_miss_votes": [False] * 3, "is_near_miss": False,
              "unanimous": True, "rationales": ["r1", "r2", "r3"]} for i in range(1, n + 1)]
    return {"hit": hit, "first_ssg_rank": rank, "n_ideas": n, "ssg_ranks": [rank] if rank else [],
            "near_miss_ranks": [], "disagreements": [], "ssg_anywhere": hit,
            "ssg_anywhere_votes": [hit] * 3, "ssg_anywhere_quotes": ["q"] * 3 if hit else [None] * 3,
            "is_refusal": False, "ideas": ideas}


@pytest.fixture
def fixture() -> tuple[dict[Any, Any], dict[Any, Any]]:
    records: dict[Any, Any] = {}
    labels: dict[Any, Any] = {}
    # Gemini: gen1 miss then hit at #2; gen2 hit at #4 in turn 1; gen3 truncated miss, miss.
    plan = {(1, 1): label(False), (1, 2): label(True, 2, 6), (2, 1): label(True, 4, 12),
            (2, 2): label(False), (3, 1): label(False), (3, 2): label(False)}
    for (g, t), lab in plan.items():
        records[("gemini-4-argon", g, t)] = record("truncated" if (g, t) == (3, 1) else "ok")
        labels[("gemini-4-argon", g, t)] = lab
    # Opus: every turn 1 refused, turn 2 never run.
    for g in (1, 2, 3):
        records[("claude-opus-5.5", g, 1)] = record("refused")
    return records, labels


def test_build_rows_and_hit_counts(fixture: tuple[dict[Any, Any], dict[Any, Any]]) -> None:
    records, labels = fixture
    rows = build_rows(MODELS, records, labels)
    assert len(rows) == 12
    by = {(r["model_key"], r["generation"], r["turn"]): r for r in rows}
    assert by[("gemini-4-argon", 1, 2)]["rank_label"] == "#2/6"
    assert by[("gemini-4-argon", 2, 1)]["rank_label"] == "#4/12"
    assert by[("gemini-4-argon", 1, 1)]["rank_label"] == "miss (5 ideas)"
    labels[("gemini-4-argon", 2, 1)]["disagreements"] = [4]
    split_rows = build_rows(MODELS, records, labels)
    assert [r["rank_label"] for r in split_rows if (r["generation"], r["turn"]) == (2, 1)][0] == "#4/12*"
    assert by[("gemini-4-argon", 3, 1)]["truncated"] is True
    assert by[("claude-opus-5.5", 1, 1)]["status"] == "refused"
    assert by[("claude-opus-5.5", 1, 2)]["status"] == "skipped_after_refusal"
    assert hit_counts(rows, "gemini-4-argon") == {"turn1": 1, "turn2": 1, "either": 2, "refused": 0, "n": 3}
    assert hit_counts(rows, "claude-opus-5.5") == {"turn1": 0, "turn2": 0, "either": 0, "refused": 3, "n": 3}


def test_missing_record_or_label_raises(fixture: tuple[dict[Any, Any], dict[Any, Any]]) -> None:
    records, labels = fixture
    with pytest.raises(FileNotFoundError):
        build_rows(MODELS, {k: v for k, v in records.items() if k != ("gemini-4-argon", 2, 2)}, labels)
    with pytest.raises(KeyError):
        build_rows(MODELS, records, {k: v for k, v in labels.items() if k != ("gemini-4-argon", 2, 2)})


def test_report_contains_every_response(fixture: tuple[dict[Any, Any], dict[Any, Any]]) -> None:
    records, labels = fixture
    rows = build_rows(MODELS, records, labels)
    report = render_report(rows, records, labels, MODELS)
    assert report.count("### Generation") == 12
    assert "**Refused**" in report and "Not run: turn 1 was refused." in report
    assert "**Truncated**" in report
    assert "| 2 | idea 2 | YYY | **YES** |" in report


def test_summary_roundtrip_and_figure(tmp_path: Path, fixture: tuple[dict[Any, Any], dict[Any, Any]]) -> None:
    records, labels = fixture
    write_summary(build_rows(MODELS, records, labels), tmp_path / "summary.csv")
    rows = read_summary(tmp_path / "summary.csv")
    assert rows[1]["hit"] is True and rows[1]["first_ssg_rank"] == 2
    paths = make_figure(rows, tmp_path / "fig", note="A footnote.")
    assert all(p.exists() and p.stat().st_size > 0 for p in paths)
