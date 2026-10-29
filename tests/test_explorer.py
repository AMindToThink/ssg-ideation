import csv
from pathlib import Path

import pytest

from ssg_ideation.explorer import idea_texts, load_reader, normalized_with_offsets
from ssg_ideation.judge import normalize

RESPONSE = """Intro line.

### 1. **Agents become one another’s trainers**
An agent can make another learner's outcomes depend on its preferred behavior.

### 2. Reputation systems
Agents learn reputation and cooperation systems.

## What I would study
Closing remarks.
"""


@pytest.mark.parametrize("text", [RESPONSE, "  a *b*   c\n\n– “q” `x`  ", "İstanbul > *x*"])
def test_normalized_with_offsets_matches_normalize(text: str) -> None:
    norm, offsets = normalized_with_offsets(text)
    assert norm == normalize(text)
    assert len(offsets) == len(norm)
    assert offsets == sorted(offsets)


def test_idea_texts_cut_at_anchors_and_closing_heading() -> None:
    texts = idea_texts(RESPONSE, ["Agents become one another's trainers", "Reputation systems Agents learn"])
    assert texts[0].startswith("### 1. **Agents become")
    assert texts[0].endswith("preferred behavior.")
    assert texts[1] == "### 2. Reputation systems\nAgents learn reputation and cooperation systems."
    with pytest.raises(ValueError):
        idea_texts(RESPONSE, ["Reputation systems Agents learn", "Agents become one another's trainers"])


def write_reader(path: Path, rows: list[dict[str, str]]) -> Path:
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["model_key", "generation", "turn", "verdict", "clear", "borderline", "note"])
        w.writeheader()
        w.writerows(rows)
    return path


def test_load_reader_checks_verdict_against_indices(tmp_path: Path) -> None:
    ok = {"model_key": "m", "generation": "1", "turn": "2", "verdict": "clear", "clear": "1 3",
          "borderline": "2", "note": "n"}
    got = load_reader(write_reader(tmp_path / "a.csv", [ok]))
    assert got[("m", 1, 2)] == {"verdict": "clear", "clear": [1, 3], "borderline": [2], "note": "n"}
    for bad in ({"verdict": "borderline"}, {"verdict": "miss"}, {"verdict": "great"}):
        with pytest.raises(ValueError):
            load_reader(write_reader(tmp_path / "b.csv", [ok | bad]))
    with pytest.raises(ValueError):
        load_reader(write_reader(tmp_path / "c.csv", [ok, ok]))
