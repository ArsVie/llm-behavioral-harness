"""El ejemplo central no debe confundir saturación aleatoria con trayectoria típica."""

from __future__ import annotations

import json

from experiments.behavior_showcase import write_showcase


def test_automatic_representative_seed_avoids_saturated_month_when_possible(tmp_path) -> None:
    outputs = write_showcase(tmp_path, seed=None, ensemble_seeds=range(30))
    trace = json.loads(outputs.trace.read_text(encoding="utf-8"))

    moods = [day["mood"] for day in trace]
    assert min(moods) > 0
    assert max(moods) < 10

