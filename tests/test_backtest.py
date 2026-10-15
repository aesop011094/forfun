import numpy as np
import pytest

from backend.backtest import chronological_split, probability_metrics, run_backtest
from backend.models import available_at, utc
from test_models import records


def test_metrics_are_computed_from_all_labels_with_defined_brier_scale():
    p = np.array([[.8, .1, .1], [.2, .5, .3], [.1, .2, .7]])
    y = np.array([0, 2, 2])
    measured = probability_metrics(p, y)
    assert measured["n"] == 3
    assert measured["log_loss"] == pytest.approx(-np.log([.8, .3, .7]).mean())
    assert measured["brier"] == pytest.approx(((p-np.eye(3)[y])**2).sum(axis=1).mean())
    assert measured["accuracy"] == pytest.approx(2/3)


def test_insufficient_data_is_not_presented_as_performance():
    report = run_backtest(records(50), "PL")
    assert report["status"] == "insufficient_data"
    assert report["models"] == []
    assert report["prediction_rows"] == []
    assert report["sample_count"] == 0


def test_walk_forward_selection_does_not_use_final_test_labels():
    history = records(360)
    split = chronological_split(history)
    report = run_backtest(history, "PL")
    assert report["status"] == "completed"
    assert report["sample_count"] == len(split["test"])
    assert len(report["prediction_rows"]) == len(split["test"])
    assert all(m["n"] == report["sample_count"] for m in report["models"])
    by_id = {match["id"]: match for match in history}
    observed, probabilities = [], []
    for row in report["prediction_rows"]:
        original = by_id[row["match_id"]]
        assert row["actual"]["home_goals"] == original["home_goals"]
        assert row["actual"]["away_goals"] == original["away_goals"]
        observed.append(0 if original["home_goals"] > original["away_goals"] else 1 if original["home_goals"] == original["away_goals"] else 2)
        probabilities.append([row["probabilities"][name] for name in ("home", "draw", "away")])
    p, y = np.asarray(probabilities), np.asarray(observed)
    ensemble = next(model for model in report["models"] if model["name"] == "ensemble")
    assert ensemble["log_loss"] == pytest.approx(-np.log(p[np.arange(len(y)), y]).mean())
    assert ensemble["brier"] == pytest.approx(((p-np.eye(3)[y])**2).sum(axis=1).mean())
    assert utc(report["inference_config"]["trained_until"]) < utc(report["test_period"]["start"])
    assert utc(report["calibration_period"]["end"]) < utc(report["validation_period"]["start"])
    ids = {row["id"] for row in split["test"]}
    altered = [{**row, "home_goals": 9, "away_goals": 0} if row["id"] in ids else dict(row) for row in history]
    changed = run_backtest(altered, "PL")
    # Later walk-forward parameters may learn from completed earlier test games,
    # but frozen selection and calibration cannot know ANY test outcome.
    assert report["ensemble_weights"] == changed["ensemble_weights"]
    assert report["inference_config"]["temperatures"] == changed["inference_config"]["temperatures"]
    assert report["feature_selection"] == changed["feature_selection"]
    assert report["prediction_rows"][0]["matrix"] == changed["prediction_rows"][0]["matrix"]
    for row in report["prediction_rows"]:
        known = [match for match in history if available_at(match) < utc(row["as_of"])]
        assert row["known_results"] == len(known)
