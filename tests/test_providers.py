from datetime import datetime, timezone
import asyncio
import hashlib
import json

import httpx

from backend import providers


NOW = datetime(2026, 10, 14, 12, tzinfo=timezone.utc)
FETCHED = "2026-10-14T12:00:00Z"
URL = "https://example.test/authorized.json"


def open_row(day="2026-10-17", score=None):
    row = {"date": day, "time": "12:30", "team1": "Arsenal FC", "team2": "Chelsea FC"}
    if score is not None:
        row["score"] = score
    return row


def parse_open(rows):
    return providers.normalize_openfootball({"matches": rows}, "PL", "2026-27", URL, FETCHED, now=NOW)


def api_row():
    return {"id": 123, "utcDate": "2026-10-13T19:00:00Z", "status": "FINISHED",
            "competition": {"code": "PL"}, "season": {"startDate": "2026-08-21"},
            "homeTeam": {"name": "Arsenal FC", "crest": "https://example.test/arsenal.svg"},
            "awayTeam": {"name": "Chelsea FC", "crest": "http://example.test/chelsea.svg"},
            "score": {"duration": "REGULAR", "fullTime": {"home": 2, "away": 1}}}


def test_date_only_kickoff_never_assumes_raw_time_timezone():
    matches, issues = parse_open([open_row()])
    assert not issues
    assert matches[0]["kickoff_precision"] == "date"
    assert matches[0]["kickoff"] == "2026-10-17T00:00:00Z"
    assert matches[0]["source_time_unverified"] == "12:30"
    assert matches[0]["official_verification"] is False
    assert matches[0]["home_goals"] is None


def test_both_public_score_schemas_and_conservative_availability():
    for score in ({"ft": [0, 0]}, [0, 0]):
        matches, issues = parse_open([open_row("2026-10-12", score)])
        assert not issues
        assert matches[0]["home_goals"] == 0
        assert matches[0]["status"] == "FINISHED"
        assert matches[0]["result_available_at"] == "2026-10-13T12:00:00Z"


def test_reject_today_future_scores_and_malformed_results():
    for day, score in (("2026-10-14", [1, 0]), ("2026-10-17", [1, 0]),
                       ("2026-10-12", [-1, 0]), ("2026-10-12", [True, 0]),
                       ("2026-10-12", [1, None])):
        matches, issues = parse_open([open_row(day, score)])
        assert not matches and len(issues) == 1


def test_bad_row_does_not_discard_remaining_dataset_and_duplicates_rejected():
    rows = [{"date": "bad"}, open_row(), open_row()]
    matches, issues = parse_open(rows)
    assert len(matches) == 1 and len(issues) == 2


def test_provider_date_change_and_aliases_do_not_change_match_identity():
    first, _ = parse_open([open_row("2026-10-17")])
    revised = open_row("2026-10-18")
    revised["team1"] = "Arsenal"
    second, _ = parse_open([revised])
    assert first[0]["id"] == second[0]["id"]
    assert providers.canonical_team("FC Internazionale Milano", "SA") == "Inter Milan"
    assert providers.canonical_team("RC Lens", "FL1") == providers.canonical_team("Racing Club de Lens", "FL1")
    assert providers.canonical_team("Atlético de Madrid", "PD") == providers.canonical_team("Club Atletico de Madrid", "PD")
    assert providers.canonical_team("Unknown Club FC", "PL") == "Unknown Club FC"


def test_api_exact_time_first_observed_availability_and_secure_crests():
    matches, issues = providers.normalize_football_data({"matches": [api_row()]}, "PL", URL, FETCHED, now=NOW)
    assert not issues
    match = matches[0]
    assert match["kickoff_precision"] == "exact"
    assert match["result_available_at"] == FETCHED
    assert match["availability_basis"] == "observed_at_fetch"
    assert match["crests"]["home"].startswith("https://")
    assert match["crests"]["away"] is None
    open_matches, _ = parse_open([open_row("2026-10-13", [2, 1])])
    assert match["id"] == open_matches[0]["id"]


def test_api_rejects_naive_timestamp_and_future_final_score():
    for timestamp in ("2026-10-13T19:00:00", "2026-10-14T11:00:00Z", "2026-10-17T19:00:00Z"):
        row = api_row()
        row["utcDate"] = timestamp
        matches, issues = providers.normalize_football_data({"matches": [row]}, "PL", URL, FETCHED, now=NOW)
        assert not matches and issues


def test_raw_cache_requires_matching_content_hash(tmp_path):
    path = tmp_path / "fixture.json"
    raw = b'{"matches": []}'
    metadata = {"sha256": hashlib.sha256(raw).hexdigest()}
    providers._save_cache(path, raw, metadata)
    assert providers._read_cache(path) == (raw, metadata)
    path.write_bytes(b"tampered")
    assert providers._read_cache(path) is None


def test_optional_api_requires_terms_without_network_or_secret_exposure(monkeypatch):
    monkeypatch.delenv("FOOTBALL_DATA_TERMS_ACCEPTED", raising=False)
    result = asyncio.run(providers.sync_football_data("sensitive-key-123", ["PL"]))
    assert not result["matches"]
    assert "條款" in result["errors"][0]
    assert "sensitive-key-123" not in json.dumps(result)


def test_http_error_does_not_echo_body_or_token():
    def fail(request):
        assert request.headers["x-auth-token"] == "sensitive-key-123"
        return httpx.Response(401, json={"debug": "sensitive-key-123"})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(fail)) as client:
            try:
                await providers._fetch(client, URL, headers={"X-Auth-Token": "sensitive-key-123"})
            except ValueError as exc:
                assert "驗證" in str(exc)
                assert "sensitive-key-123" not in str(exc)
            else:
                raise AssertionError("unauthorized response accepted")
    asyncio.run(run())
