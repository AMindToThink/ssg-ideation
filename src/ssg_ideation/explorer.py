"""Export the data behind the interactive explorer (a claude.ai dashboard artifact).

Writes results/explorer/responses.json (one row per model, generation and turn)
and results/explorer/ideas.json (one row per idea in the judge's segmentation,
with the idea's text cut from the response). The reader's labels (the Claude Code
Opus 5.5 read) come from results/reader_labels.csv, which transcribes
results/claude_code_opus_spotcheck.md onto the judge's idea numbering.

Usage:
    uv run python -m ssg_ideation.explorer
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from ssg_ideation.analyze import LABEL_LETTER, SUMMARY_PATH, load_labels, load_reader, load_records
from ssg_ideation.client import write_json_atomic
from ssg_ideation.config import RESULTS_DIR, TARGET_MODELS
from ssg_ideation.judge import _QUOTES, judge_content
from ssg_ideation.plot import read_summary

EXPLORER_DIR: Path = RESULTS_DIR / "explorer"


def normalized_with_offsets(text: str) -> tuple[str, list[int]]:
    """judge.normalize(text), plus the offset in `text` of each output character."""
    out: list[str] = []
    offsets: list[int] = []
    for i, ch in enumerate(text.translate(_QUOTES)):
        if ch in "*_#`>\\":
            continue
        if ch.isspace():
            if not out or out[-1] == " ":
                continue
            ch = " "
        for c in ch.lower():
            out.append(c)
            offsets.append(i)
    if out and out[-1] == " ":
        out.pop()
        offsets.pop()
    return "".join(out), offsets


def idea_texts(response: str, anchors: list[str]) -> list[str]:
    """Each idea's text: from the start of its anchor's line to the next idea's.

    An idea whose line also holds the previous idea's anchor (a run-on list such
    as "Runners-up: X ...; Y ...") starts at its own anchor instead. The last
    idea stops at the next horizontal rule or level-1/2 heading, so a closing
    section ("What I'd study") is not attributed to it.
    """
    haystack, offsets = normalized_with_offsets(response)
    starts: list[int] = []
    pos = 0
    prev_anchor = -1
    for anchor in anchors:
        needle, _ = normalized_with_offsets(anchor)
        found = haystack.find(needle, pos)
        if found < 0:
            raise ValueError(f"anchor not found in order: {anchor!r}")
        pos = found + 1
        line_start = response.rfind("\n", 0, offsets[found]) + 1
        starts.append(line_start if line_start > prev_anchor else offsets[found])
        prev_anchor = offsets[found]
    ends = starts[1:] + [len(response)]
    tail = [response.find(m, starts[-1] + 1) for m in ("\n---", "\n## ", "\n# ")] if starts else []
    if any(t >= 0 for t in tail):
        ends[-1] = min(t for t in tail if t >= 0)
    return [response[s:e].strip().removesuffix("---").strip() for s, e in zip(starts, ends, strict=True)]


def build(
    summary: list[dict[str, Any]],
    records: dict[tuple[str, int, int], dict[str, Any]],
    labels: dict[tuple[str, int, int], dict[str, Any]],
    reader: dict[tuple[str, int, int], dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(responses rows, ideas rows). Every run response must have a reader label."""
    responses: list[dict[str, Any]] = []
    ideas: list[dict[str, Any]] = []
    for s in summary:
        k = (s["model_key"], s["generation"], s["turn"])
        key = "|".join(map(str, k))
        row: dict[str, Any] = {
            "key": key, "model_key": s["model_key"], "model": s["model"], "generation": s["generation"],
            "turn": s["turn"], "status": s["status"], "judge_hit": s["hit"],
            "judge_rank": s["first_ssg_rank"], "n_ideas": s["n_ideas"],
            "judge_split": s["rank_label"].endswith("*"), "judge_ssg": s["ssg_ranks"] or "",
            "judge_near": s["near_miss_ranks"] or "",
            "judge_borderline": s["borderline_ranks"] or "",
            "judge_hit_any": s["judge_hit_any"], "judge_rank_any": s["judge_rank_any"],
            "cost_usd": float(s["cost_usd"]) if s["cost_usd"] else None,
            "reasoning_tokens": int(s["reasoning_tokens"]) if s["reasoning_tokens"] else None,
            "completion_tokens": int(s["completion_tokens"]) if s["completion_tokens"] else None,
            "latency_s": float(s["latency_s"]) if s["latency_s"] else None,
        }
        rd = reader.get(k)
        if s["status"] == "skipped_after_refusal":
            rd = {"verdict": "not run", "clear": [], "borderline": [], "note": ""}
        if rd is None:
            raise KeyError(f"no reader label for {k}")
        n = s["n_ideas"] or 0
        if any(not 1 <= i <= n for i in rd["clear"] + rd["borderline"]):
            raise ValueError(f"{k}: reader index outside 1..{n}")
        row |= {
            "reader_verdict": rd["verdict"],
            "reader_clear": " ".join(map(str, rd["clear"])),
            "reader_borderline": " ".join(map(str, rd["borderline"])),
            "reader_rank_clear": min(rd["clear"], default=None),
            "reader_rank_any": min(rd["clear"] + rd["borderline"], default=None),
            "reader_note": rd["note"],
        }
        responses.append(row)
        lab = labels.get(k)
        if lab is None or lab.get("api_refused"):
            continue
        texts = idea_texts(judge_content(records[k]), [i["anchor"] for i in lab["ideas"]])
        for idea, text in zip(lab["ideas"], texts, strict=True):
            i = idea["index"]
            ideas.append({
                "key": key, "index": i, "title": idea["title"],
                "votes": "".join("Y" if v else "n" for v in idea["votes"]),
                "is_ssg": idea["is_ssg"],
                "near_votes": "".join("Y" if v else "n" for v in idea["near_miss_votes"]),
                "is_near_miss": idea["is_near_miss"],
                "label": idea["label"],
                "labels": "".join(LABEL_LETTER[x] for x in idea["labels"]),
                "reader": "clear" if i in rd["clear"] else "borderline" if i in rd["borderline"] else "",
                **{f"rationale_{j + 1}": r for j, r in enumerate(idea["rationales"])},
                "text": text,
            })
    return responses, ideas


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=EXPLORER_DIR)
    args = ap.parse_args(argv)
    keys = [m.key for m in TARGET_MODELS]
    responses, ideas = build(read_summary(SUMMARY_PATH), load_records(keys), load_labels(), load_reader())
    write_json_atomic(args.out / "responses.json", responses)
    write_json_atomic(args.out / "ideas.json", ideas)
    print(f"wrote {len(responses)} responses and {len(ideas)} ideas to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
