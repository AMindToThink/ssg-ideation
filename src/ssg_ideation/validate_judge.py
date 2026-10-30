"""Compare a judge's labels with the Claude Code Opus 5.5 reader labels.

The reader labels (results/reader_labels.csv) were written by Claude Opus 5.5 in Claude
Code and spot-checked by Matthew, who sided with them where he checked. They are the
reference here. Ranks are on the DeepSeek segmentation's idea numbering in both.

Pass criteria (exit 1 otherwise):
- every response the reader calls clear is a clear hit, and its first clear idea is
  one the reader calls clear;
- no response the reader calls borderline or miss is a clear hit (the five DeepSeek
  false positives are among these);
- every idea the reader calls clear is labeled clear or borderline.

Borderline agreement is reported but not required.

Usage:
    uv run python -m ssg_ideation.validate_judge [--labels results/opus_judge_labels.json]
"""

import argparse
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from ssg_ideation.analyze import load_labels, load_reader
from ssg_ideation.opus_judge import OPUS_JUDGE, OPUS_LABELS_PATH

Key = tuple[str, int, int]

# DeepSeek hits that the reader labels do not count (results/claude_code_opus_spotcheck.md).
KNOWN_FALSE_POSITIVES: dict[Key, list[int]] = {
    ("claude-fable-5.1", 1, 2): [11, 19],
    ("claude-fable-5.1", 1, 3): [1],
    ("gpt-6-astra", 1, 1): [3],
    ("gemini-4-argon", 2, 3): [2],
    ("gemini-4-argon", 3, 1): [2],
}


def idea_label(idea: dict[str, Any]) -> str:
    """The idea's label; the DeepSeek judge's binary labels map to clear/near_miss/none."""
    if "label" in idea:
        return idea["label"]
    return "clear" if idea["is_ssg"] else "near_miss" if idea["is_near_miss"] else "none"


def judge_verdict(lab: dict[str, Any]) -> str:
    if lab["hit"]:
        return "clear"
    return "borderline" if lab.get("hit_any") else "miss"


def compare(reader: dict[Key, dict[str, Any]], labels: dict[Key, dict[str, Any]]) -> dict[str, Any]:
    """Response-level confusion, per-response rows and the list of failed criteria."""
    rows: list[dict[str, Any]] = []
    failures: list[str] = []
    confusion: Counter[tuple[str, str]] = Counter()
    for k, rd in sorted(reader.items()):
        if rd["verdict"] == "refused":
            continue
        if k not in labels:
            raise KeyError(f"no judge label for {k}")
        lab = labels[k]
        jv = judge_verdict(lab)
        confusion[(rd["verdict"], jv)] += 1
        by_index = {i["index"]: i for i in lab["ideas"]}
        row = {"key": k, "reader": rd["verdict"], "judge": jv, "reader_clear": rd["clear"],
               "reader_borderline": rd["borderline"], "judge_clear": lab["ssg_ranks"],
               "judge_borderline": lab.get("borderline_ranks", []),
               "fp_ideas": {i: idea_label(by_index[i]) for i in KNOWN_FALSE_POSITIVES.get(k, [])}}
        rows.append(row)
        if rd["verdict"] == "clear":
            if not lab["hit"]:
                failures.append(f"{k}: reader clear at {rd['clear']}, judge not clear")
            elif lab["first_ssg_rank"] not in rd["clear"]:
                failures.append(f"{k}: judge's first clear idea {lab['first_ssg_rank']} is not "
                                f"reader-clear {rd['clear']}")
        elif lab["hit"]:
            failures.append(f"{k}: reader {rd['verdict']}, judge clear at {lab['ssg_ranks']}")
        for i in rd["clear"]:
            if idea_label(by_index[i]) not in ("clear", "borderline"):
                failures.append(f"{k}: reader-clear idea {i} labeled {idea_label(by_index[i])}")
    return {"rows": rows, "confusion": confusion, "failures": failures}


def render(result: dict[str, Any]) -> str:
    verdicts = ("clear", "borderline", "miss")
    c = result["confusion"]
    out = ["Response-level verdicts (rows: reader, columns: judge)",
           f"{'':12}" + "".join(f"{v:>12}" for v in verdicts)]
    for r in verdicts:
        out.append(f"{r:12}" + "".join(f"{c[(r, j)]:>12}" for j in verdicts))
    agree = sum(c[(v, v)] for v in verdicts)
    total = sum(c.values())
    out.append(f"exact agreement {agree}/{total}; clear-hit agreement "
               f"{sum(n for (r, j), n in c.items() if (r == 'clear') == (j == 'clear'))}/{total}")
    out.append("")
    out.append("Known DeepSeek false positives, labels now:")
    for row in result["rows"]:
        if row["fp_ideas"]:
            out.append(f"  {row['key']}: {row['fp_ideas']}")
    out.append("")
    out.append("Per response (reader clear|borderline  vs  judge clear|borderline):")
    for row in result["rows"]:
        mark = "" if row["reader"] == row["judge"] else "   <-- differs"
        out.append(f"  {row['key']}: reader {row['reader']} {row['reader_clear']}|"
                   f"{row['reader_borderline']}  judge {row['judge']} {row['judge_clear']}|"
                   f"{row['judge_borderline']}{mark}")
    out.append("")
    out.append("FAILED: " + "; ".join(result["failures"]) if result["failures"] else "PASSED")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--labels", type=Path, nargs="+", default=[OPUS_LABELS_PATH],
                    help="one or more label files; their responses must not overlap")
    ap.add_argument("--models", nargs="+", default=None,
                    help="compare only these models' responses (default: every reader-labeled response)")
    ap.add_argument("--judge-model", default=OPUS_JUDGE.model_id)
    args = ap.parse_args(argv)
    labels: dict[Key, dict[str, Any]] = {}
    for path in args.labels:
        more = load_labels(path, args.judge_model)
        if set(more) & set(labels):
            raise ValueError(f"{path} repeats responses already loaded")
        labels |= more
    reader = load_reader()
    if args.models is not None:
        reader = {k: v for k, v in reader.items() if k[0] in args.models}
    result = compare(reader, labels)
    print(render(result))
    return 1 if result["failures"] else 0


if __name__ == "__main__":
    sys.exit(main())
