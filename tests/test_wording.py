"""The second reader of the responses is Claude Opus 5.5 in Claude Code, not a person.

No tracked file may describe that reading as one done by a person.
"""

import re
import subprocess

from ssg_ideation.config import REPO_ROOT

# Built from parts so this file does not contain the phrases it forbids.
HAND = "h" + "and"
MANUAL = "man" + "ual"
BY = "b" + "y"
FORBIDDEN = re.compile(
    rf"\b{HAND}[\s_-]*(read|label|audit|written|check)|\b{BY}\s+{HAND}\b|\b{MANUAL}[\s_-]*spot",
    re.IGNORECASE,
)
BINARY_SUFFIXES = (".png", ".pdf", ".woff2", ".gz")


def test_no_tracked_file_calls_the_claude_read_a_human_one() -> None:
    files = subprocess.run(
        ["git", "ls-files"], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    ).stdout.split("\n")
    hits = []
    for name in filter(None, files):
        if name.endswith(BINARY_SUFFIXES):
            continue
        path = REPO_ROOT / name
        if not path.is_file():
            continue
        text = path.read_text(errors="ignore")
        hits += [f"{name}: {m.group(0)!r}" for m in FORBIDDEN.finditer(text)]
        if FORBIDDEN.search(name):
            hits.append(f"file name {name}")
    assert hits == []


def test_pattern_catches_the_old_wording() -> None:
    for bad in [f"{HAND} read", f"{HAND.title()}-read position", f"labels written {BY} {HAND}",
                f"a {HAND} audit", f"{MANUAL}_spotcheck.md", f"{HAND} labels"]:
        assert FORBIDDEN.search(bad), bad
    for ok in ["handoff notes", "on the other hand", "Claude Code Opus 5.5 read", "handle"]:
        assert not FORBIDDEN.search(ok), ok
