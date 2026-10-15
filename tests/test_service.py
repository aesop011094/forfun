"""Persistence/service edge cases; synthetic records are test inputs only."""
from datetime import datetime, timezone

import pytest

from backend.database import Database
from backend.service import FootballService, known_history


def record():
    return {"id": "unit-match", "league": "PL", "season": "2025-26", "home": "Unit Home", "away": "Unit Away",
            "kickoff": "2026-01-01T00:00:00+00:00", "kickoff_precision": "date", "status": "FINISHED",
            "home_goals": 1, "away_goals": 0, "source": "unit-test-only", "source_url": "about:unit-test",
            "fetched_at": "2026-01-03T00:00:00+00:00", "result_available_at": "2026-01-02T12:00:00+00:00"}


def test_ingest_keeps_first_result_observation_and_records_corrections(tmp_path):
    db = Database(tmp_path / "football.db")
    original = record()
    db.ingest([original])
    db.ingest([{**original, "result_available_at": "2026-01-05T00:00:00+00:00", "fetched_at": "2026-01-05T00:00:00+00:00"}])
    assert db.match(original["id"])["result_available_at"] == original["result_available_at"]
    changed = {**original, "home_goals": 2, "result_available_at": "2026-01-06T00:00:00+00:00", "fetched_at": "2026-01-06T00:00:00+00:00"}
    db.ingest([changed])
    assert db.match(original["id"])["result_available_at"] == changed["result_available_at"]
    with db.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM match_revisions").fetchone()[0] == 1
    db.ingest([{**original, "status": "SCHEDULED", "home_goals": None, "away_goals": None}])
    assert db.match(original["id"])["home_goals"] == 2


def test_service_uses_matrix_as_authority_and_preserves_original_context(tmp_path):
    service = FootballService(Database(tmp_path / "football.db"))
    fixture = {**record(), "status": "SCHEDULED", "home_goals": None, "away_goals": None}
    raw = {"matrix": [[.1, .2], [.3, .4]], "expected_goals": {"home": 99, "away": 99},
           "probabilities": {"home": 1, "draw": 0, "away": 0}}
    snapshot = service.make_prediction(fixture, "2025-12-30T00:00:00+00:00", raw, [])
    assert snapshot["probabilities"] == pytest.approx({"home": .3, "draw": .5, "away": .2})
    assert snapshot["expected_goals"] == pytest.approx({"home": .7, "away": .6})
    fixture["kickoff"] = "2026-01-04T00:00:00+00:00"
    assert snapshot["match_snapshot"]["kickoff"] == "2026-01-01T00:00:00+00:00"
    assert snapshot["data_quality"]["score"] == 0
    assert snapshot["confidence"]["score"] == 0


def test_correction_never_rewrites_settled_original_and_is_disclosed(tmp_path):
    db = Database(tmp_path / "football.db")
    service = FootballService(db)
    match = record()
    db.ingest([match])
    snapshot = {"id": "unit-prediction", "created_at": "2025-12-31T00:00:00+00:00", "as_of": "2025-12-31T00:00:00+00:00",
                "model_version": "unit-1", "historical": False, "probabilities": {"home": .6, "draw": .2, "away": .2}}
    db.save_prediction(match["id"], snapshot)
    assert service.settle() == 1
    first = db.history()["predictions"][0]
    assert first["actual"]["home_goals"] == 1
    assert first["outcome"]["correct"]
    db.ingest([{**match, "away_goals": 2, "fetched_at": "2026-01-07T00:00:00+00:00", "result_available_at": "2026-01-07T00:00:00+00:00"}])
    assert service.settle() == 0
    amended = db.history()["predictions"][0]
    assert amended["actual"]["away_goals"] == 0
    assert amended["actual_correction"]["away_goals"] == 2
    assert amended["probabilities"] == snapshot["probabilities"]


def test_no_history_labels_available_at_cutoff_are_admitted():
    match = record()
    assert known_history([match], match["result_available_at"]) == []
    assert known_history([match], "2026-01-02T12:00:01+00:00") == [match]


def test_no_current_day_date_only_or_started_fixture_predictions(tmp_path, monkeypatch):
    service = FootballService(Database(tmp_path / "football.db"))
    today = datetime.now(timezone.utc).date().isoformat()
    fixture = {**record(), "kickoff": today + "T23:00:00+00:00", "status": "SCHEDULED", "home_goals": None, "away_goals": None}
    service.db.ingest([fixture])
    # No model is allowed to run for an uncertain current-day kickoff.
    def forbidden_engine(*_args, **_kwargs):
        raise AssertionError("Current-day date-only fixture must never reach model")
    monkeypatch.setattr(service, "engine", forbidden_engine)
    output = service.predict_upcoming()
    assert output["created"] == 0
    assert service.db.predictions(fixture["id"]) == []
