"""Independent acceptance checks for the isolated, user-entered analysis workspace.

All match rows below are deliberately synthetic test fixtures. They are never
application data, training evidence, or claimed forecasting performance.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import sqlite3

import numpy as np
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def manual_app(tmp_path, monkeypatch):
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "import-only.db"))
    from backend.main import create_app

    return create_app(db_path=tmp_path / "manual.db", scheduler=False)


@pytest.fixture
def manual_client(manual_app):
    with TestClient(manual_app, base_url="http://localhost", client=("127.0.0.1", 49000)) as client:
        yield client


def scenario(**overrides):
    return {
        "mode": "scenario", "league": "PL", "home": "Manual Home", "away": "Manual Away",
        "kickoff": "2027-06-01T20:00:00+08:00", "home_expected_goals": 2.2, "away_expected_goals": .8,
        "source_note": "Synthetic QA input; not a real fixture or verified football data.",
        "notes": "", **overrides,
    }


def sample_history(n=90):
    teams = ["Unit A", "Unit B", "Unit C", "Unit D", "Unit E", "Unit F"]
    base = datetime(2022, 1, 1, tzinfo=timezone.utc)
    rows = []
    for i in range(n):
        kickoff = base + timedelta(days=i * 2)
        home = teams[i % len(teams)]
        away = teams[(i + 1 + i // len(teams)) % len(teams)]
        if away == home:
            away = teams[(teams.index(home) + 3) % len(teams)]
        rows.append({
            "kickoff": kickoff.isoformat(), "home": home, "away": away,
            "home_goals": (i * 7 + 1) % 4, "away_goals": (i * 11) % 3,
            "available_at": (kickoff + timedelta(hours=36)).isoformat(),
        })
    return rows


def custom(**overrides):
    return {
        "mode": "custom_history", "league": "PL", "home": "Unit A", "away": "Unit B",
        "kickoff": "2022-07-01T12:00:00+00:00", "historical_matches": sample_history(60),
        "source_note": "Synthetic QA history; user supplied and unverified.", "notes": "", **overrides,
    }


def official_rows(rows):
    return [
        {"id": f"qa-official-{i}", "league": "PL", "season": "2021-22", "home": row["home"],
         "away": row["away"], "kickoff": row["kickoff"], "kickoff_precision": "exact", "status": "FINISHED",
         "home_goals": row["home_goals"], "away_goals": row["away_goals"],
         "result_available_at": row["available_at"], "source": "unit-test-only",
         "source_url": "about:unit-test", "fetched_at": "2022-09-01T00:00:00+00:00"}
        for i, row in enumerate(rows)
    ]


def analyze(client, value):
    response = client.post("/api/manual/analyses", json=value)
    assert response.status_code == 201, response.text
    return response.json()


def assert_consistent(prediction):
    """Reconcile every market independently against the returned score distribution."""
    matrix = np.asarray(prediction["matrix"])
    assert matrix.ndim == 2 and min(matrix.shape) > 0
    assert np.isfinite(matrix).all() and (matrix >= 0).all()
    assert matrix.sum() == pytest.approx(1, abs=1e-9)
    probabilities = prediction["probabilities"]
    assert probabilities["home"] == pytest.approx(np.tril(matrix, -1).sum(), abs=1e-9)
    assert probabilities["draw"] == pytest.approx(np.trace(matrix), abs=1e-9)
    assert probabilities["away"] == pytest.approx(np.triu(matrix, 1).sum(), abs=1e-9)
    assert sum(probabilities.values()) == pytest.approx(1, abs=1e-9)
    h, a = np.indices(matrix.shape)
    assert prediction["expected_goals"]["home"] == pytest.approx((matrix * h).sum(), abs=1e-7)
    assert prediction["expected_goals"]["away"] == pytest.approx((matrix * a).sum(), abs=1e-7)
    markets = prediction["markets"]
    for market in markets["over_under"]:
        assert market["over"] == pytest.approx(matrix[h + a > market["line"]].sum(), abs=1e-9)
        assert market["over"] + market["under"] == pytest.approx(1, abs=1e-9)
    assert markets["btts"]["yes"] == pytest.approx(matrix[1:, 1:].sum(), abs=1e-9)
    assert markets["btts"]["yes"] + markets["btts"]["no"] == pytest.approx(1, abs=1e-9)
    assert sum(row["probability"] for row in markets["total_goals"]) == pytest.approx(1, abs=1e-9)
    for row in markets["scores"]:
        assert row["probability"] == pytest.approx(matrix[row["home"], row["away"]], abs=1e-9)
    for row in markets["asian_handicap"]:
        assert sum(row[key] for key in ("full_win", "half_win", "push", "half_loss", "full_loss")) == pytest.approx(1, abs=1e-9)


def test_scenario_is_coherent_uncalibrated_and_not_claimed_as_observed_xg(manual_client):
    result = analyze(manual_client, scenario())
    assert result["mode"] == "scenario"
    assert result["input"]["source_note"] == scenario()["source_note"]
    assert result["provenance"] and result["warnings"]
    prediction = result["prediction"]
    assert_consistent(prediction)
    assert prediction["expected_goals"] == pytest.approx({"home": 2.2, "away": .8}, abs=1e-5)
    assert prediction["confidence"]["score"] == 0
    assert prediction["data_quality"]["score"] == 0
    assert any(term in prediction["confidence"]["label"] for term in ("校準", "驗證"))
    assert result["data_gaps"]
    # The rates are user assumptions, not observed expected-goal event data.
    assert not any("真實 xG" in str(value) for value in prediction["data_quality"]["available"])


def test_zero_rates_produce_certain_goalless_scenario_without_clipping(manual_client):
    prediction = analyze(manual_client, scenario(home_expected_goals=0, away_expected_goals=0))["prediction"]
    assert_consistent(prediction)
    assert prediction["probabilities"] == pytest.approx({"home": 0, "draw": 1, "away": 0})
    assert prediction["markets"]["btts"]["yes"] == 0
    assert prediction["markets"]["btts"]["no"] == 1
    assert prediction["expected_goals"] == {"home": 0, "away": 0}
    assert len(prediction["markets"]["scores"]) == 5
    assert next(row for row in prediction["markets"]["total_goals"] if row["goals"] == 0)["probability"] == 1


def test_large_scenario_rates_are_not_silently_capped_by_fitted_model_limits(manual_client):
    prediction = analyze(manual_client, scenario(home_expected_goals=9.5, away_expected_goals=10))["prediction"]
    assert_consistent(prediction)
    assert prediction["expected_goals"] == pytest.approx({"home": 9.5, "away": 10}, abs=1e-6)
    assert prediction["confidence"]["score"] == 0


def test_scenario_notes_do_not_change_probabilities_or_become_confirmed_features(manual_client):
    first = analyze(manual_client, scenario())
    note = "使用者聲稱：虛構球員 QA Striker 受傷，主隊必勝。未核實。"
    second = analyze(manual_client, scenario(notes=note))
    assert second["input"]["notes"] == note
    assert second["prediction"]["probabilities"] == first["prediction"]["probabilities"]
    assert np.asarray(second["prediction"]["matrix"]) == pytest.approx(np.asarray(first["prediction"]["matrix"]))
    assert "QA Striker" not in json.dumps(second["prediction"]["data_quality"], ensure_ascii=False)
    assert "QA Striker" not in json.dumps(second["comparison"], ensure_ascii=False)
    assert "QA Striker" not in json.dumps(second["report"], ensure_ascii=False)


def test_analysis_persists_immutably_without_polluting_official_data(manual_client, manual_app):
    db = manual_app.state.service.db
    before = {table: None for table in ("matches", "predictions", "reports", "settlements")}
    with db.connect() as connection:
        for table in before:
            before[table] = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    first = analyze(manual_client, scenario())
    second = analyze(manual_client, scenario(home_expected_goals=1.1))
    assert first["id"] != second["id"]
    assert manual_client.get(f"/api/manual/analyses/{first['id']}").json() == first
    listed = manual_client.get("/api/manual/analyses").json()
    assert listed["total"] == 2
    assert {item["id"] for item in listed["analyses"]} == {first["id"], second["id"]}
    paged = manual_client.get("/api/manual/analyses?limit=1&offset=1").json()
    assert paged["total"] == 2 and len(paged["analyses"]) == 1
    assert paged["analyses"][0]["id"] != listed["analyses"][0]["id"]
    assert manual_client.get("/api/manual/analyses/not-a-record").status_code == 404
    with db.connect() as connection:
        for table, count in before.items():
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == count
    for statement, parameters in [
        ("UPDATE manual_analyses SET payload=? WHERE id=?", ("{}", first["id"])),
        ("DELETE FROM manual_analyses WHERE id=?", (first["id"],)),
    ]:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            with db.connect() as connection:
                connection.execute(statement, parameters)
    with db.connect() as connection:
        connection.execute("INSERT OR REPLACE INTO manual_analyses(id,created_at,mode,payload) VALUES(?,?,?,?)",
                           (first["id"], first["created_at"], first["mode"], "{}"))
    assert db.manual_analysis(first["id"]) == first
    from backend.database import Database
    assert Database(db.path).manual_analysis(first["id"]) == first


def test_custom_history_trains_from_supplied_rows_and_changes_with_results(manual_client):
    low = custom()
    high = custom()
    for row in low["historical_matches"]:
        if row["home"] == "Unit A":
            row["home_goals"] = 0
    for row in high["historical_matches"]:
        if row["home"] == "Unit A":
            row["home_goals"] = 7
    first = analyze(manual_client, low)
    second = analyze(manual_client, high)
    assert_consistent(first["prediction"])
    assert_consistent(second["prediction"])
    assert second["prediction"]["expected_goals"]["home"] > first["prediction"]["expected_goals"]["home"]
    assert second["prediction"]["probabilities"] != first["prediction"]["probabilities"]
    assert first["warnings"] and first["provenance"]
    assert manual_client.get("/api/matches?window=all").json()["matches"] == []
    assert manual_client.get("/api/performance").json()["reports"] == []


@pytest.mark.parametrize("zero_targets", ["both", "away"])
def test_custom_history_with_zero_goal_targets_does_not_crash_unused_candidate_models(manual_client, zero_targets):
    rows = sample_history(160)
    for row in rows:
        row["away_goals"] = 0
        if zero_targets == "both":
            row["home_goals"] = 0
    result = analyze(manual_client, custom(kickoff="2023-01-01T12:00:00+00:00", historical_matches=rows))
    assert_consistent(result["prediction"])
    assert result["prediction"]["confidence"]["score"] == 0
    assert result["provenance"]["validated"] is False


@pytest.mark.parametrize("mutation", ["future_kickoff", "unavailable_result", "availability_before_finish", "duplicate", "same_team_overlap", "too_few", "unknown_team"])
def test_custom_history_refuses_temporal_leakage_conflicts_and_insufficient_samples(manual_client, mutation):
    value = custom()
    if mutation == "future_kickoff":
        value["historical_matches"][-1]["kickoff"] = "2022-07-02T12:00:00+00:00"
        value["historical_matches"][-1]["available_at"] = "2022-07-04T00:00:00+00:00"
    elif mutation == "unavailable_result":
        value["historical_matches"][-1]["available_at"] = value["kickoff"]
    elif mutation == "availability_before_finish":
        value["historical_matches"][0]["available_at"] = value["historical_matches"][0]["kickoff"]
    elif mutation == "duplicate":
        value["historical_matches"].append(dict(value["historical_matches"][0]))
    elif mutation == "same_team_overlap":
        value["historical_matches"].append({**value["historical_matches"][0], "away": "Unit F"})
    elif mutation == "too_few":
        value["historical_matches"] = value["historical_matches"][:10]
    elif mutation == "unknown_team":
        value["home"] = "No Historical Samples"
    response = manual_client.post("/api/manual/analyses", json=value)
    assert response.status_code == 422, response.text
    assert isinstance(response.json()["detail"], str)
    assert manual_client.get("/api/manual/analyses").json()["total"] == 0


def test_known_team_analysis_uses_only_available_results_before_target_kickoff(manual_client, manual_app):
    db = manual_app.state.service.db
    db.ingest(official_rows(sample_history(90)))
    value = {"mode": "history", "league": "PL", "home": "Unit A", "away": "Unit B",
             "kickoff": "2022-07-01T12:00:00+00:00", "notes": "", "source_note": "Synthetic fixture"}
    first = analyze(manual_client, value)
    later = official_rows(sample_history(130))[90:]
    for row in later:
        row["home_goals"] = 12
        row["away_goals"] = 0
    db.ingest(later)
    second = analyze(manual_client, value)
    assert np.asarray(second["prediction"]["matrix"]) == pytest.approx(np.asarray(first["prediction"]["matrix"]), abs=1e-10)
    assert second["prediction"]["probabilities"] == pytest.approx(first["prediction"]["probabilities"], abs=1e-10)
    assert datetime.fromisoformat(second["prediction"]["as_of"]) <= datetime.fromisoformat(value["kickoff"])


@pytest.mark.parametrize("overrides", [
    {"home": "Manual Away"}, {"home": "   "}, {"kickoff": "2027-06-01T20:00:00"},
    {"kickoff": "not-a-date"}, {"home_expected_goals": -1}, {"away_expected_goals": 10.01},
    {"home_expected_goals": None}, {"league": "UNKNOWN"}, {"unexpected_field": "reject-me"},
    {"historical_matches": sample_history(1)},
])
def test_bad_scenario_inputs_have_clear_validation_and_are_not_saved(manual_client, overrides):
    response = manual_client.post("/api/manual/analyses", json=scenario(**overrides))
    assert response.status_code == 422, response.text
    assert isinstance(response.json()["detail"], str)
    assert manual_client.get("/api/manual/analyses").json()["total"] == 0


@pytest.mark.parametrize("invalid_number", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_rate_never_causes_server_error_or_invalid_snapshot(manual_client, invalid_number):
    # Raw JSON deliberately exercises the server; strict client serializers
    # normally prevent sending these invalid JSON numeric extensions.
    response = manual_client.post("/api/manual/analyses", content=json.dumps(scenario(home_expected_goals=invalid_number)),
                                  headers={"Content-Type": "application/json"})
    assert response.status_code == 422, response.text
    assert manual_client.get("/api/manual/analyses").json()["total"] == 0


@pytest.mark.parametrize("score", [1.5, "2", True, -1, 31])
def test_historical_scores_are_bounded_integers_not_coerced(manual_client, score):
    value = custom()
    value["historical_matches"][0]["home_goals"] = score
    response = manual_client.post("/api/manual/analyses", json=value)
    assert response.status_code == 422, response.text


def test_manual_workspace_enforces_local_or_token_access_and_same_origin(manual_app, monkeypatch):
    with TestClient(manual_app, base_url="https://football.example", client=("203.0.113.12", 49001)) as client:
        for method, path in [("get", "/api/manual/analyses"), ("get", "/api/manual/analyses/missing"), ("post", "/api/manual/analyses")]:
            response = getattr(client, method)(path, **({"json": scenario()} if method == "post" else {}))
            assert response.status_code == 403
        monkeypatch.setenv("ADMIN_TOKEN", "synthetic-qa-admin-only")
        assert client.get("/api/manual/analyses").status_code == 401
        headers = {"X-Admin-Token": "synthetic-qa-admin-only", "Origin": "https://football.example"}
        response = client.post("/api/manual/analyses", json=scenario(), headers=headers)
        assert response.status_code == 201, response.text
        analysis_id = response.json()["id"]
        assert client.get(f"/api/manual/analyses/{analysis_id}", headers=headers).status_code == 200
        assert client.get("/api/manual/analyses", headers={**headers, "Origin": "https://another.example"}).status_code == 403
        assert client.post("/api/manual/analyses", json=scenario(), headers={**headers, "Referer": "https://another.example/path"}).status_code == 403

