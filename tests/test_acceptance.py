"""Independent QA: coherent markets, chronological inference, immutable data.

Small synthetic inputs here are explicitly unit-test fixtures, never application
records or evidence of historical forecasting performance.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import importlib
import json

import numpy as np
import pytest


def model_module():
    return importlib.import_module('backend.models')


def test_all_markets_are_derived_from_same_normalized_distribution():
    models = model_module()
    matrix = np.zeros((5, 5))
    # Deliberately asymmetric, including goalless, draw, large score and BTTS.
    for home, away, p in [(0, 0, .07), (0, 1, .14), (1, 0, .19), (1, 1, .20), (2, 1, .11), (1, 3, .13), (4, 2, .16)]:
        matrix[home, away] = p
    markets = models.markets_from_matrix(matrix)
    assert sum(matrix.flatten()) == pytest.approx(1)
    for market in markets['over_under']:
        threshold = market['line']
        expected = sum(matrix[h, a] for h in range(5) for a in range(5) if h + a > threshold)
        assert market['over'] == pytest.approx(expected)
        assert market['under'] == pytest.approx(1 - expected)
    assert markets['btts']['yes'] == pytest.approx(matrix[1:, 1:].sum())
    assert markets['btts']['yes'] + markets['btts']['no'] == pytest.approx(1)
    assert sum(row['probability'] for row in markets['total_goals']) == pytest.approx(1)
    for row in markets['total_goals']:
        expected = sum(matrix[h, a] for h in range(5) for a in range(5) if h + a == row['goals'])
        assert row['probability'] == pytest.approx(expected)
    assert len(markets['scores']) == 5
    for score in markets['scores']:
        assert score['probability'] == pytest.approx(matrix[score['home'], score['away']])
    assert [s['probability'] for s in markets['scores']] == sorted([s['probability'] for s in markets['scores']], reverse=True)
    for market in markets['asian_handicap']:
        assert sum(market[key] for key in ('full_win', 'half_win', 'push', 'half_loss', 'full_loss')) == pytest.approx(1)


@pytest.mark.parametrize('goal_difference,line,expected', [
    (0, 0, 'push'),
    (0, -.25, 'half_loss'),
    (0, .25, 'half_win'),
    (1, -1, 'push'),
    (1, -1.25, 'half_loss'),
    (1, -.75, 'half_win'),
    (1, -.5, 'full_win'),
    (-1, .75, 'half_loss'),
    (-1, 1.25, 'half_win'),
    (-1, 1, 'push'),
    (0, -.5, 'full_loss'),
    (0, .5, 'full_win'),
])
def test_asian_handicap_handles_push_and_quarter_settlement(goal_difference, line, expected):
    matrix = np.zeros((4, 4))
    home, away = max(goal_difference, 0), max(-goal_difference, 0)
    matrix[home, away] = 1
    settled = model_module().asian_handicap(matrix, line)
    for key in ('full_win', 'half_win', 'push', 'half_loss', 'full_loss'):
        assert settled[key] == pytest.approx(int(key == expected)), (goal_difference, line, settled)


def historical_fixture(n=120):
    """Explicit reproducible test fixture with genuine availability cutoffs."""
    teams = ['Unit A', 'Unit B', 'Unit C', 'Unit D', 'Unit E', 'Unit F']
    base = datetime(2022, 1, 1, tzinfo=timezone.utc)
    rows = []
    for i in range(n):
        kickoff = base + timedelta(days=i * 2)
        home = teams[i % len(teams)]
        away = teams[(i + 1 + i // len(teams)) % len(teams)]
        if away == home:
            away = teams[(teams.index(home) + 3) % len(teams)]
        rows.append({'id': f'unit-{i}', 'league': 'PL', 'season': '2022-23', 'home': home, 'away': away,
                     'kickoff': kickoff.isoformat(), 'kickoff_precision': 'date', 'status': 'FINISHED',
                     'home_goals': (i * 7 + 1) % 4, 'away_goals': (i * 11) % 3,
                     'result_available_at': (kickoff + timedelta(hours=36)).isoformat(),
                     'source': 'unit-test-only', 'source_url': 'about:unit-test',
                     'fetched_at': (base + timedelta(days=500)).isoformat()})
    return rows


def test_future_and_not_yet_available_results_cannot_change_prediction():
    models = model_module()
    history = historical_fixture()
    cutoff = '2022-10-01T00:00:00+00:00'
    first = models.ModelEngine(history, cutoff).predict('Unit A', 'Unit B', 'PL')
    injected = dict(history[-1], id='unit-future', home='Unit A', away='Unit B',
                    kickoff='2022-10-02T00:00:00+00:00', result_available_at='2022-10-03T12:00:00+00:00', home_goals=9, away_goals=0)
    late_result = dict(history[0], id='unit-late-published', home='Unit A', away='Unit B', home_goals=8,
                       result_available_at=cutoff)
    second = models.ModelEngine(history + [injected, late_result], cutoff).predict('Unit A', 'Unit B', 'PL')
    assert np.asarray(second['matrix']) == pytest.approx(np.asarray(first['matrix']), abs=1e-10)
    assert second['probabilities'] == pytest.approx(first['probabilities'], abs=1e-10)


def test_prediction_probabilities_expected_goals_and_markets_are_consistent():
    result = model_module().ModelEngine(historical_fixture(), '2022-10-01T00:00:00+00:00').predict('Unit A', 'Unit B', 'PL')
    matrix = np.asarray(result['matrix'])
    assert np.isfinite(matrix).all()
    assert (matrix >= 0).all()
    assert matrix.sum() == pytest.approx(1, abs=1e-8)
    probabilities = result['probabilities']
    assert sum(probabilities.values()) == pytest.approx(1, abs=1e-8)
    assert probabilities['home'] == pytest.approx(np.tril(matrix, -1).sum())
    assert probabilities['away'] == pytest.approx(np.triu(matrix, 1).sum())
    assert probabilities['draw'] == pytest.approx(np.trace(matrix))
    assert result['expected_goals']['home'] == pytest.approx((matrix * np.arange(matrix.shape[0])[:, None]).sum(), abs=1e-6)
    assert result['expected_goals']['away'] == pytest.approx((matrix * np.arange(matrix.shape[1])[None, :]).sum(), abs=1e-6)


def test_data_source_documentation_discloses_license_and_historical_limits():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    text = (root / 'DATA_SOURCES.md').read_text()
    assert 'CC0' in text
    assert 'football-data.org' in text
    assert any(word in text.lower() for word in ('retrospect', '回溯', '歷史資料'))
    assert any(word in text.lower() for word in ('36', '發布', 'publication'))


def snapshot_fixture(snapshot_id='snapshot-one', as_of='2022-01-01T00:00:00+00:00'):
    return {'id': snapshot_id, 'created_at': as_of, 'as_of': as_of, 'model_version': 'qa-model-1', 'historical': True,
            'probabilities': {'home': .4, 'draw': .3, 'away': .3}, 'source': 'unit-test-only'}


def test_prediction_snapshots_are_immutable_but_versions_can_append(tmp_path):
    import sqlite3
    from backend.database import Database
    db = Database(tmp_path / 'qa.db')
    match = historical_fixture(1)[0]
    db.ingest([match])
    original = snapshot_fixture()
    assert db.save_prediction(match['id'], original)
    assert not db.save_prediction(match['id'], {**original, 'probabilities': {'home': 1, 'draw': 0, 'away': 0}})
    assert db.predictions(match['id']) == [original]
    with pytest.raises(sqlite3.IntegrityError, match='immutable'):
        with db.connect() as connection:
            connection.execute('UPDATE predictions SET payload=? WHERE id=?', ('{}', original['id']))
    with pytest.raises(sqlite3.IntegrityError, match='immutable'):
        with db.connect() as connection:
            connection.execute('DELETE FROM predictions WHERE id=?', (original['id'],))
    revised = snapshot_fixture('snapshot-two', '2022-01-01T01:00:00+00:00')
    assert db.save_prediction(match['id'], revised)
    assert len(db.predictions(match['id'])) == 2
    assert db.predictions(match['id'])[-1] == original


def test_match_result_updates_do_not_rewrite_original_prediction(tmp_path):
    from backend.database import Database
    db = Database(tmp_path / 'qa.db')
    match = historical_fixture(1)[0]
    db.ingest([{**match, 'status': 'SCHEDULED', 'home_goals': None, 'away_goals': None}])
    original = snapshot_fixture()
    db.save_prediction(match['id'], original)
    db.ingest([match])
    history = db.history()['predictions'][0]
    assert history['probabilities'] == original['probabilities']
    assert history['as_of'] == original['as_of']
    assert history['match']['home_goals'] == match['home_goals']
    assert history['actual'] is None  # ingest does not manufacture a settlement


def test_read_only_api_does_not_expose_keys_and_reports_empty_data_honestly(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setenv('DATABASE_PATH', str(tmp_path / 'default-api-qa.db'))
    from backend.main import create_app
    secret = 'qa-not-a-real-api-key-sensitive-marker'
    monkeypatch.setenv('FOOTBALL_DATA_API_KEY', secret)
    monkeypatch.setenv('ADMIN_TOKEN', secret + '-admin')
    app = create_app(db_path=tmp_path / 'api-qa.db', scheduler=False)
    with TestClient(app, base_url='http://localhost', client=('127.0.0.1', 49000)) as client:
        for endpoint in ('/health', '/api/status', '/api/settings', '/api/matches', '/api/history', '/api/performance', '/api/teams'):
            response = client.get(endpoint)
            assert response.status_code == 200, (endpoint, response.text)
            assert secret not in response.text
        status = client.get('/api/status').json()
        assert status['counts']['matches'] == 0
        assert client.get('/api/matches').json()['matches'] == []
        assert client.get('/api/performance').json()['reports'] == []
        assert client.get('/api/history').json()['predictions'] == []
        assert client.get('/api/matches/does-not-exist').status_code == 404
        assert client.post('/api/admin/predict').status_code in (401, 403)


def test_public_remote_caller_cannot_start_admin_job_without_admin_token(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setenv('DATABASE_PATH', str(tmp_path / 'default-remote-qa.db'))
    from backend.main import create_app
    monkeypatch.delenv('ADMIN_TOKEN', raising=False)
    app = create_app(db_path=tmp_path / 'remote-qa.db', scheduler=False)
    with TestClient(app, base_url='https://football.example', client=('203.0.113.23', 49001)) as client:
        response = client.post('/api/admin/predict')
        assert response.status_code in (401, 403)
        assert client.get('/api/status').json()['jobs'] == []


def test_training_row_features_do_not_contain_its_own_or_later_scores():
    models = model_module()
    original = historical_fixture(18)
    changed = [dict(row) for row in original]
    changed[9]['home_goals'] = 12
    changed[9]['away_goals'] = 0
    changed[14]['home_goals'] = 8
    features_original, labels_original = models.training_features(original)
    features_changed, labels_changed = models.training_features(changed)
    assert features_changed[:10] == pytest.approx(features_original[:10])
    assert labels_changed[9].tolist() != labels_original[9].tolist()
    # Earlier fixtures on the same day cannot see a same-day completed result.
    simultaneous = [dict(row) for row in original[:2]]
    simultaneous[1]['kickoff'] = simultaneous[0]['kickoff']
    simultaneous[1]['result_available_at'] = simultaneous[0]['result_available_at']
    x, _ = models.training_features(simultaneous)
    assert x[0, 0] == pytest.approx(x[1, 0])


def test_settlement_uses_actual_score_and_latest_prospective_snapshot_once(tmp_path):
    import math
    from backend.database import Database
    from backend.service import FootballService
    db = Database(tmp_path / 'settlement-qa.db')
    match = historical_fixture(1)[0]
    db.ingest([{**match, 'status': 'SCHEDULED', 'home_goals': None, 'away_goals': None}])
    older = {**snapshot_fixture('early', '2021-12-31T22:00:00+00:00'), 'historical': False,
             'probabilities': {'home': .1, 'draw': .2, 'away': .7}}
    final = {**snapshot_fixture('final', '2021-12-31T23:00:00+00:00'), 'historical': False}
    db.save_prediction(match['id'], older)
    db.save_prediction(match['id'], final)
    db.ingest([match])
    service = FootballService(db)
    assert service.settle() == 2
    assert service.settle() == 0
    rows = {row['id']: row for row in db.history()['predictions']}
    assert rows['final']['actual']['home_goals'] == match['home_goals']
    assert rows['final']['actual']['away_goals'] == match['away_goals']
    assert rows['final']['actual']['source'] == 'unit-test-only'
    assert rows['final']['outcome']['log_loss'] == pytest.approx(-math.log(.4))
    assert rows['final']['outcome']['brier'] == pytest.approx(.6**2 + .3**2 + .3**2)
    monitor = service.monitoring()
    assert monitor['sample_count'] == 1
    assert monitor['log_loss'] == pytest.approx(-math.log(.4))
    assert monitor['accuracy'] == 1
    assert monitor['drift_alert'] is None
    assert rows['early']['probabilities'] == older['probabilities']


def test_reported_probability_metrics_match_independent_formula():
    from backend.backtest import probability_metrics
    p = np.asarray([[.6, .25, .15], [.2, .3, .5], [.1, .7, .2]])
    y = np.asarray([0, 1, 2])
    result = probability_metrics(p, y)
    assert result['n'] == 3
    assert result['log_loss'] == pytest.approx(-np.log([.6, .3, .2]).mean())
    expected_brier = np.mean([sum((p[i, j] - int(y[i] == j))**2 for j in range(3)) for i in range(3)])
    assert result['brier'] == pytest.approx(expected_brier)
    assert result['accuracy'] == pytest.approx(1 / 3)
    assert sum(bin['n'] for bin in result['calibration_bins']) == 9


def test_insufficient_history_does_not_manufacture_backtest_metrics():
    from backend.backtest import run_backtest
    report = run_backtest(historical_fixture(20), 'PL')
    assert report['status'] == 'insufficient_data'
    assert report['models'] == []
    assert report['prediction_rows'] == []
    assert not report['inference_config'].get('validated')


def test_confirmed_final_snapshot_is_saved_before_kickoff_once(tmp_path, monkeypatch):
    import backend.service as service_module
    from backend.database import Database
    from backend.service import FootballService
    service = FootballService(Database(tmp_path / 'final-qa.db'))
    history = historical_fixture(120)
    fixture = {**history[0], 'id': 'unit-final-fixture', 'home': 'Unit A', 'away': 'Unit B',
               'status': 'SCHEDULED', 'kickoff': '2022-10-01T00:10:00+00:00', 'kickoff_precision': 'exact',
               'home_goals': None, 'away_goals': None, 'result_available_at': None}
    service.db.ingest(history + [fixture])
    clock = ['2022-10-01T00:00:00+00:00']
    monkeypatch.setattr(service_module, 'now', lambda: clock[0])

    class UnitEngine:
        def predict(self, *_args, **_kwargs):
            return {'matrix': [[.2, .3], [.4, .1]], 'expected_goals': {'home': .5, 'away': .4},
                    'probabilities': {'home': .4, 'draw': .3, 'away': .3}}

    monkeypatch.setattr(service, 'engine', lambda *_args: (UnitEngine(), history))
    assert service.predict_upcoming(final_only=True)['created'] == 1
    final = service.db.predictions(fixture['id'])[0]
    assert final['final_snapshot'] is True
    assert datetime.fromisoformat(final['as_of']) < datetime.fromisoformat(fixture['kickoff'])
    assert final['match_snapshot']['kickoff'] == fixture['kickoff']
    clock[0] = '2022-10-01T00:05:00+00:00'
    assert service.predict_upcoming(final_only=True)['created'] == 0
    clock[0] = '2022-10-01T00:11:00+00:00'
    assert service.predict_upcoming()['created'] == 0
    assert service.db.predictions(fixture['id']) == [final]


def test_admin_prediction_job_reaches_completion_and_missing_api_key_is_clear(tmp_path, monkeypatch):
    import time
    from fastapi.testclient import TestClient
    monkeypatch.setenv('DATABASE_PATH', str(tmp_path / 'default-job-qa.db'))
    monkeypatch.setenv('ADMIN_TOKEN', 'qa-admin-only')
    monkeypatch.delenv('FOOTBALL_DATA_API_KEY', raising=False)
    from backend.main import create_app
    with TestClient(create_app(db_path=tmp_path / 'job-qa.db', scheduler=False), base_url='http://localhost', client=('127.0.0.1', 49005)) as client:
        headers = {'X-Admin-Token': 'qa-admin-only'}
        blocked = client.post('/api/admin/sync', json={'provider': 'football-data.org'}, headers=headers)
        assert blocked.status_code == 422
        assert 'FOOTBALL_DATA_API_KEY' in blocked.json()['detail']
        queued = client.post('/api/admin/predict', headers=headers)
        assert queued.status_code == 202
        job_id = queued.json()['job_id']
        for _ in range(100):
            job = client.get(f'/api/jobs/{job_id}').json()
            if job['status'] in ('completed', 'failed'):
                break
            time.sleep(.01)
        assert job['status'] == 'completed'
        assert job['result']['created'] == 0
        assert job['result']['errors'] == []
        assert client.get('/api/status').json()['counts']['matches'] == 0
