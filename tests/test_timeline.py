from pathlib import Path
from typing import Any

from ssg_ideation.config import TIMELINE, model_by_key
from ssg_ideation.timeline import MEASURES, make_figure, pro_note, unavailable_note


def counts_with(hits: dict[str, dict[str, int]] | None = None) -> list[dict[str, Any]]:
    hits = hits or {}
    return [{"key": p.key, "label": model_by_key(p.key).label, "released": p.released, "pro": p.pro,
             "route": p.route, **{m: hits.get(p.key, {}).get(m, 0) for m in MEASURES}, "n": 3, "refused": 0}
            for p in TIMELINE]


def test_timeline_figure_renders(tmp_path: Path) -> None:
    paths = make_figure(counts_with({"gpt-5": {"turn2": 1, "either": 1}}), "A note.", tmp_path, "t")
    assert all(p.exists() and p.stat().st_size > 0 for p in paths)


def test_pro_note_names_every_pro_and_lists_only_measures_where_pro_wins() -> None:
    note = pro_note(counts_with({"gpt-5.4": {"turn1": 1}, "gpt-5.4-pro": {"turn3": 1},
                                 "gpt-5.5": {"turn2": 2}, "gpt-5.5-pro": {"turn2": 1}}))
    assert all(model_by_key(p.key).label in note for p in TIMELINE if p.pro)
    assert note.endswith("except turn 3, where GPT-5.4 Pro had 1 and GPT-5.4 had 0.")


def test_pro_note_without_wins() -> None:
    assert pro_note(counts_with()).endswith("each of which did the same as or worse than its base model.")


def test_unavailable_note() -> None:
    assert unavailable_note().startswith("Not tested: GPT-4 Turbo (Nov 2023), o1 (Dec 2024), o1-pro (Mar 2025)")
