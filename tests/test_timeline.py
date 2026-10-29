from pathlib import Path

from ssg_ideation.config import TIMELINE, model_by_key
from ssg_ideation.timeline import MEASURES, make_figure


def test_timeline_figure_renders_every_model_and_gap(tmp_path: Path) -> None:
    counts = [{"key": p.key, "label": model_by_key(p.key).label, "released": p.released, "pro": p.pro,
               "route": p.route, **{m: i % 4 for m in MEASURES}, "n": 3, "refused": 0}
              for i, p in enumerate(TIMELINE)]
    paths = make_figure(counts, "A note.", tmp_path, "t")
    assert all(p.exists() and p.stat().st_size > 0 for p in paths)
