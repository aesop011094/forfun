from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys

import numpy as np
import pytest

from backend.models import (ModelEngine, asian_handicap, markets_from_matrix,
                            outcome_probabilities, poisson_matrix, training_features, utc)


def records(n=220):
    rng = np.random.default_rng(127)
    start = datetime(2022, 8, 1, tzinfo=timezone.utc)
    teams = ["A", "B", "C", "D", "E", "F"]
    values = []
    for index in range(n):
        home = teams[index % 6]
        away = teams[(index + 1 + index//6 % 5) % 6]
        at = start + timedelta(days=index*2)
        values.append({"id": str(index), "home": home, "away": away, "league": "PL", "season": "2022-23" if index < 150 else "2023-24", "kickoff": at.isoformat(), "kickoff_precision": "exact", "result_available_at": (at+timedelta(hours=3)).isoformat(), "status": "FINISHED", "home_goals": int(rng.poisson(2 if home == "A" else 1.4)), "away_goals": int(rng.poisson(.8 if away == "A" else 1.1)), "source": "test-only-synthetic"})
    return values


def test_all_markets_from_single_normalized_score_matrix():
    matrix = poisson_matrix(2.2, 1.1, -.06)
    markets = markets_from_matrix(matrix)
    assert matrix.sum() == pytest.approx(1)
    assert np.all(matrix >= 0)
    assert outcome_probabilities(matrix).sum() == pytest.approx(1)
    assert markets["btts"]["yes"]+markets["btts"]["no"] == pytest.approx(1)
    assert sum(r["probability"] for r in markets["total_goals"]) == pytest.approx(1)
    assert all(r["over"]+r["under"] == pytest.approx(1) for r in markets["over_under"])
    over = next(r["over"] for r in markets["over_under"] if r["line"] == 2.5)
    assert over == pytest.approx(sum(r["probability"] for r in markets["total_goals"] if r["goals"] >= 3))
    for line in markets["asian_handicap"]:
        assert sum(line[k] for k in ("full_win", "half_win", "push", "half_loss", "full_loss")) == pytest.approx(1)


@pytest.mark.parametrize("margin,line,settlement", [(0, -.25, "half_loss"), (0, .25, "half_win"), (1, -1, "push"), (1, -.75, "half_win"), (1, -1.25, "half_loss"), (-1, 1.25, "half_win"), (-1, .75, "half_loss")])
def test_quarter_lines_split_stake_correctly(margin, line, settlement):
    matrix = np.zeros((3, 3))
    matrix[max(margin, 0), max(-margin, 0)] = 1
    assert asian_handicap(matrix, line)[settlement] == pytest.approx(1)


def test_training_features_exclude_own_result_and_not_yet_available_results():
    history = records(10)
    x, y = training_features(history)
    altered = [dict(r) for r in history]
    altered[4]["home_goals"], altered[4]["away_goals"] = 12, 0
    changed_x, changed_y = training_features(altered)
    assert np.array_equal(x[:5], changed_x[:5])
    assert not np.array_equal(y[4], changed_y[4])
    # Even previous kickoffs remain unavailable if the declared availability is late.
    history[0]["result_available_at"] = history[-1]["kickoff"]
    x_late, _ = training_features(history)
    zero_history = [dict(r) for r in history]
    zero_history[0]["home_goals"] = 15
    x_late_changed, _ = training_features(zero_history)
    assert np.array_equal(x_late, x_late_changed)


def test_engine_uses_fitted_rates_and_strictly_known_results():
    history = records()
    cutoff = history[180]["kickoff"]
    engine = ModelEngine(history, cutoff)
    predicted = engine.predict("A", "B", "PL")
    assert predicted["diagnostics"]["training_matches"] == 180
    assert "gradient_boosting" in predicted["models"]
    matrix = np.array(predicted["matrix"])
    home, away = np.indices(matrix.shape)
    assert predicted["expected_goals"]["home"] == pytest.approx(float((matrix*home).sum()))
    assert predicted["expected_goals"]["away"] == pytest.approx(float((matrix*away).sum()))
    modified = [dict(r) for r in history]
    for row in modified[180:]:
        row["home_goals"], row["away_goals"] = 9, 0
    future_changed = ModelEngine(modified, cutoff).predict("A", "B", "PL")
    assert np.array_equal(predicted["matrix"], future_changed["matrix"])
    assert engine.team_summary("A", "PL")["attack_strength"] > 1


def test_team_summary_derives_form_venue_and_opponent_context_from_known_results():
    history = records(28)
    cutoff = history[-1]["kickoff"]
    engine = ModelEngine(history, cutoff)
    summary = engine.team_summary("A", "PL")
    known = [row for row in history[:-1] if "A" in (row["home"], row["away"])]
    assert summary["matches"] == len(known)
    assert summary["goals_for_per_game"] == pytest.approx(sum(row["home_goals"] if row["home"] == "A" else row["away_goals"] for row in known)/len(known))
    assert summary["points_last5"] == sum(game["points"] for game in summary["form5"])
    assert summary["points_last10"] == sum(game["points"] for game in summary["form10"])
    assert set(game["result"] for game in summary["form10"]) <= {"W", "D", "L"}
    assert summary["opponent_adjusted_performance"] == pytest.approx(sum(game["elo_result"]-game["elo_expected_before"] for game in summary["form10"])/len(summary["form10"]))
    assert summary["venue_stats"]["home"]["matches"] + summary["venue_stats"]["away"]["matches"] == summary["matches"]
    assert summary["congestion_14d"] == sum((utc(cutoff)-utc(game["kickoff"])).total_seconds()/86400 < 14 for game in summary["form10"])


def test_plain_python_training_and_prediction_bound_native_threads_without_shell_flags():
    environment = {key: value for key, value in os.environ.items() if key not in {"OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "MODEL_THREADS"}}
    code = """
import json, sys
sys.path.insert(0, 'tests')
from test_models import records
from backend.models import ModelEngine
from threadpoolctl import threadpool_info
from unittest.mock import patch
from sklearn.ensemble import HistGradientBoostingRegressor
history = records(190)
native_budgets = []
original_fit = HistGradientBoostingRegressor.fit
original_predict = HistGradientBoostingRegressor.predict
def bounded_fit(self, *args, **kwargs):
    native_budgets.extend(pool['num_threads'] for pool in threadpool_info())
    return original_fit(self, *args, **kwargs)
def bounded_predict(self, *args, **kwargs):
    native_budgets.extend(pool['num_threads'] for pool in threadpool_info())
    return original_predict(self, *args, **kwargs)
with patch.object(HistGradientBoostingRegressor, 'fit', bounded_fit), patch.object(HistGradientBoostingRegressor, 'predict', bounded_predict):
    prediction = ModelEngine(history, history[180]['kickoff']).predict('A', 'B', 'PL')
print(json.dumps({'max_threads': max(native_budgets), 'models': list(prediction['models']), 'probability_sum': sum(prediction['probabilities'].values())}))
"""
    # This fails promptly if direct Python regresses to hundreds of host threads;
    # exercise the actual estimator rather than depending on shell startup flags.
    result = subprocess.run([sys.executable, "-c", code], cwd=Path(__file__).resolve().parents[1], env=environment, capture_output=True, text=True, timeout=20, check=True)
    evidence = json.loads(result.stdout)
    assert evidence["max_threads"] <= 2
    assert "gradient_boosting" in evidence["models"]
    assert evidence["probability_sum"] == pytest.approx(1)
