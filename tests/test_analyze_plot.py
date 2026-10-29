from pathlib import Path
from typing import Any

import pytest

from ssg_ideation.analyze import build_rows, hit_counts, render_report, visible_reasoning, write_summary
from ssg_ideation.config import TURN3_PROMPT
from ssg_ideation.plot import drop_unanswered, make_figure, read_summary, with_claude_labels

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
    # Gemini: gen1 miss, hit at #2, kept at #1 of its top 3; gen2 hit at #4 in turn 1, dropped
    # from its top 3; gen3 truncated miss, miss, then SSG first appears in its top 3 at #2.
    plan = {(1, 1): label(False), (1, 2): label(True, 2, 6), (1, 3): label(True, 1, 3),
            (2, 1): label(True, 4, 12), (2, 2): label(False), (2, 3): label(False, n=3),
            (3, 1): label(False), (3, 2): label(False), (3, 3): label(True, 2, 3)}
    for (g, t), lab in plan.items():
        records[("gemini-4-argon", g, t)] = record("truncated" if (g, t) == (3, 1) else "ok")
        labels[("gemini-4-argon", g, t)] = lab
    # Opus: every turn 1 refused, turns 2 and 3 never run.
    for g in (1, 2, 3):
        records[("claude-opus-5.5", g, 1)] = record("refused")
    return records, labels


def test_build_rows_and_hit_counts(fixture: tuple[dict[Any, Any], dict[Any, Any]]) -> None:
    records, labels = fixture
    rows = build_rows(MODELS, records, labels)
    assert len(rows) == 18
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
    assert by[("claude-opus-5.5", 1, 3)]["status"] == "skipped_after_refusal"
    assert by[("gemini-4-argon", 1, 3)]["rank_label"] == "#1/3"
    assert hit_counts(rows, "gemini-4-argon") == {
        "turn1": 1, "turn2": 1, "either": 2, "turn3": 2, "turn3_new": 1, "refused": 0, "n": 3
    }
    assert hit_counts(rows, "claude-opus-5.5") == {
        "turn1": 0, "turn2": 0, "either": 0, "turn3": 0, "turn3_new": 0, "refused": 3, "n": 3
    }


def test_missing_record_or_label_raises(fixture: tuple[dict[Any, Any], dict[Any, Any]]) -> None:
    records, labels = fixture
    with pytest.raises(FileNotFoundError):
        build_rows(MODELS, {k: v for k, v in records.items() if k != ("gemini-4-argon", 2, 2)}, labels)
    with pytest.raises(FileNotFoundError):
        build_rows(MODELS, {k: v for k, v in records.items() if k != ("gemini-4-argon", 2, 3)}, labels)
    with pytest.raises(KeyError):
        build_rows(MODELS, records, {k: v for k, v in labels.items() if k != ("gemini-4-argon", 2, 2)})


def test_report_contains_every_response(fixture: tuple[dict[Any, Any], dict[Any, Any]]) -> None:
    records, labels = fixture
    rows = build_rows(MODELS, records, labels)
    report = render_report(rows, records, labels, MODELS)
    assert report.count("### Generation") == 18
    assert "**Refused**" in report and "Not run: an earlier turn was refused." in report
    assert f"**Turn 3:** {TURN3_PROMPT}" in report
    assert "| Gemini 4 Argon | Judge (DeepSeek V4.1 Flash) | 1/3 | 1/3 | 2/3 | 2/3 | 1 | 0 |" in report
    assert "**Truncated**" in report
    assert "| 2 | idea 2 | YYY | **YES** |" in report


def test_summary_roundtrip_and_figure(tmp_path: Path, fixture: tuple[dict[Any, Any], dict[Any, Any]]) -> None:
    records, labels = fixture
    write_summary(build_rows(MODELS, records, labels), tmp_path / "summary.csv")
    rows = read_summary(tmp_path / "summary.csv")
    assert rows[1]["hit"] is True and rows[1]["first_ssg_rank"] == 2
    paths = make_figure(rows, tmp_path / "fig", note="A footnote.")
    assert all(p.exists() and p.stat().st_size > 0 for p in paths)


def test_figure_drops_models_with_no_answered_call(fixture: tuple[dict[Any, Any], dict[Any, Any]]) -> None:
    records, labels = fixture
    rows = build_rows(MODELS, records, labels)
    kept, dropped = drop_unanswered(rows)
    assert dropped == ["Claude Opus 5.5"]
    assert {r["model_key"] for r in kept} == {"gemini-4-argon"}
    assert len(kept) == 9


def test_visible_reasoning_falls_back_to_summaries() -> None:
    assert visible_reasoning({"reasoning": "plain", "reasoning_details": []}) == "plain"
    details = [{"type": "reasoning.encrypted", "data": "x"},
               {"type": "reasoning.summary", "summary": "first"},
               {"type": "reasoning.summary", "summary": "second"}]
    assert visible_reasoning({"reasoning": "", "reasoning_details": details}) == "first\n\nsecond"
    assert visible_reasoning({"reasoning": None, "reasoning_details": details[:1]}) is None


def test_claude_labels_replace_judge_hits(fixture: tuple[dict[Any, Any], dict[Any, Any]], tmp_path: Path) -> None:
    records, labels = fixture
    rows = build_rows(MODELS, records, labels)
    none: dict[str, list[int]] = {"clear": [], "borderline": []}
    reader = {(r["model_key"], r["generation"], r["turn"]): dict(none) for r in rows}
    reader[("gemini-4-argon", 1, 2)] = {"clear": [], "borderline": [3]}
    reader[("gemini-4-argon", 3, 1)] = {"clear": [5], "borderline": [2]}
    strict = {(r["generation"], r["turn"]): r for r in with_claude_labels(rows, reader) if r["model_key"] == "gemini-4-argon"}
    assert strict[(1, 2)]["hit"] is False and strict[(2, 1)]["hit"] is False
    assert strict[(3, 1)]["first_ssg_rank"] == 5 and strict[(3, 1)]["rank_label"] == "#5/5"
    loose = {(r["generation"], r["turn"]): r for r in with_claude_labels(rows, reader, borderline=True)
             if r["model_key"] == "gemini-4-argon"}
    assert loose[(1, 2)]["first_ssg_rank"] == 3 and loose[(3, 1)]["first_ssg_rank"] == 2
    assert hit_counts(with_claude_labels(rows, reader), "gemini-4-argon")["turn1"] == 1
    paths = make_figure(with_claude_labels(rows, reader), tmp_path, name="claude")
    assert [p.name for p in paths] == ["claude.png", "claude.pdf"]


def test_claude_columns_counts_and_report_order(fixture: tuple[dict[Any, Any], dict[Any, Any]], tmp_path: Path) -> None:
    records, labels = fixture
    none = {"verdict": "miss", "clear": [], "borderline": [], "note": ""}
    reader = {("gemini-4-argon", g, t): dict(none) for g in (1, 2, 3) for t in (1, 2, 3)}
    reader |= {("claude-opus-5.5", g, 1): dict(none, verdict="refused") for g in (1, 2, 3)}
    reader[("gemini-4-argon", 1, 2)] = {"verdict": "borderline", "clear": [], "borderline": [3], "note": "b"}
    reader[("gemini-4-argon", 3, 1)] = {"verdict": "clear", "clear": [5], "borderline": [2], "note": "Says it."}
    rows = build_rows(MODELS, records, labels, reader)
    by = {(r["model_key"], r["generation"], r["turn"]): r for r in rows}
    want = {"claude_verdict": "clear", "claude_hit": True, "claude_rank": 5, "claude_hit_any": True, "claude_rank_any": 2}
    assert {k: by[("gemini-4-argon", 3, 1)][k] for k in want} == want
    assert by[("claude-opus-5.5", 1, 2)]["claude_verdict"] == "not run"
    assert hit_counts(rows, "gemini-4-argon", "claude")["turn1"] == 1
    assert hit_counts(rows, "gemini-4-argon", "claude_any")["turn2"] == 1
    assert hit_counts(rows, "gemini-4-argon", "judge")["turn2"] == 1
    report = render_report(rows, records, labels, MODELS)
    claude_row = report.index("| Gemini 4 Argon | Claude Code Opus 5.5 (spot-checked), clear |")
    judge_row = report.index("| Gemini 4 Argon | Judge (DeepSeek V4.1 Flash) |")
    assert claude_row < judge_row
    section = report[report.index("### Generation 3, turn 1"):]
    assert section.index("Claude Code Opus 5.5 (spot-checked):** clear statement, first at idea 5. Says it.") < section.index("**Judge:**")
    write_summary(rows, tmp_path / "s.csv")
    back = read_summary(tmp_path / "s.csv")
    fields = list(back[0])
    assert fields.index("claude_verdict") < fields.index("hit")
    assert [r for r in back if r["generation"] == 3 and r["turn"] == 1 and r["model_key"] == "gemini-4-argon"][0]["claude_rank"] == 5
    with pytest.raises(KeyError):
        build_rows(MODELS, records, labels, {k: v for k, v in reader.items() if k != ("gemini-4-argon", 2, 2)})
