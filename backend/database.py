"""SQLite persistence. Prediction snapshots are protected by database triggers."""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

ROOT = Path(__file__).resolve().parents[1]


def encode(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


class Database:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path or os.environ.get("DATABASE_PATH", ROOT / "data/football.db"))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA recursive_triggers=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS matches (
                    id TEXT PRIMARY KEY, league TEXT NOT NULL, season TEXT NOT NULL,
                    home TEXT NOT NULL, away TEXT NOT NULL, kickoff TEXT NOT NULL,
                    status TEXT NOT NULL, result_available_at TEXT, payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS matches_league_date ON matches(league,kickoff);
                CREATE TABLE IF NOT EXISTS match_revisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, match_id TEXT NOT NULL REFERENCES matches(id),
                    observed_at TEXT NOT NULL, previous_payload TEXT NOT NULL, current_payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS predictions (
                    id TEXT PRIMARY KEY, match_id TEXT NOT NULL REFERENCES matches(id),
                    created_at TEXT NOT NULL, as_of TEXT NOT NULL, model_version TEXT NOT NULL,
                    historical INTEGER NOT NULL CHECK(historical IN (0,1)), payload TEXT NOT NULL,
                    UNIQUE(match_id,as_of,model_version,historical)
                );
                CREATE INDEX IF NOT EXISTS predictions_match ON predictions(match_id,created_at);
                CREATE TRIGGER IF NOT EXISTS immutable_prediction_update
                    BEFORE UPDATE ON predictions BEGIN
                    SELECT RAISE(ABORT,'Prediction snapshots are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS immutable_prediction_delete
                    BEFORE DELETE ON predictions BEGIN
                    SELECT RAISE(ABORT,'Prediction snapshots are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS immutable_prediction_replace
                    BEFORE INSERT ON predictions WHEN EXISTS(SELECT 1 FROM predictions WHERE id=NEW.id)
                    BEGIN SELECT RAISE(IGNORE); END;
                CREATE TRIGGER IF NOT EXISTS immutable_prediction_unique_replace
                    BEFORE INSERT ON predictions WHEN EXISTS(SELECT 1 FROM predictions
                        WHERE match_id=NEW.match_id AND as_of=NEW.as_of AND model_version=NEW.model_version AND historical=NEW.historical)
                    BEGIN SELECT RAISE(IGNORE); END;
                CREATE TABLE IF NOT EXISTS settlements (
                    prediction_id TEXT PRIMARY KEY REFERENCES predictions(id),
                    settled_at TEXT NOT NULL, actual TEXT NOT NULL, outcome TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS reports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, league TEXT NOT NULL,
                    created_at TEXT NOT NULL, payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS reports_league ON reports(league,created_at);
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL, status TEXT NOT NULL,
                    created_at TEXT NOT NULL, started_at TEXT, completed_at TEXT, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS source_status (
                    name TEXT PRIMARY KEY, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS manual_analyses (
                    id TEXT PRIMARY KEY, created_at TEXT NOT NULL,
                    mode TEXT NOT NULL CHECK(mode IN ('history','custom_history','scenario')),
                    payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS manual_analyses_created ON manual_analyses(created_at);
                CREATE TRIGGER IF NOT EXISTS immutable_manual_analysis_update
                    BEFORE UPDATE ON manual_analyses BEGIN
                    SELECT RAISE(ABORT,'Manual analysis snapshots are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS immutable_manual_analysis_delete
                    BEFORE DELETE ON manual_analyses BEGIN
                    SELECT RAISE(ABORT,'Manual analysis snapshots are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS immutable_manual_analysis_replace
                    BEFORE INSERT ON manual_analyses WHEN EXISTS(SELECT 1 FROM manual_analyses WHERE id=NEW.id)
                    BEGIN SELECT RAISE(IGNORE); END;
            """)

    def ingest(self, matches: list[dict]) -> int:
        with self.connect() as db:
            for match in matches:
                match = dict(match)
                existing = db.execute("SELECT payload FROM matches WHERE id=?", (match["id"],)).fetchone()
                if existing:
                    previous = json.loads(existing[0])
                    same_result = previous.get("status") == match.get("status") == "FINISHED" and all(previous.get(k) == match.get(k) for k in ("home_goals", "away_goals"))
                    if same_result and previous.get("result_available_at"):
                        match["result_available_at"] = min(previous["result_available_at"], match.get("result_available_at") or previous["result_available_at"])
                    # A stale fixture source cannot silently erase a known final result.
                    if previous.get("status") == "FINISHED" and match.get("status") != "FINISHED":
                        continue
                    relevant = ("kickoff", "status", "home_goals", "away_goals", "home", "away", "source", "kickoff_precision")
                    if any(previous.get(k) != match.get(k) for k in relevant):
                        db.execute("INSERT INTO match_revisions(match_id,observed_at,previous_payload,current_payload) VALUES(?,?,?,?)",
                                   (match["id"], match["fetched_at"], encode(previous), encode(match)))
                db.execute("""INSERT INTO matches
                    (id,league,season,home,away,kickoff,status,result_available_at,payload)
                    VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                    league=excluded.league,season=excluded.season,home=excluded.home,
                    away=excluded.away,kickoff=excluded.kickoff,status=excluded.status,
                    result_available_at=excluded.result_available_at,payload=excluded.payload""",
                    (match["id"], match["league"], match["season"], match["home"], match["away"],
                     match["kickoff"], match["status"], match.get("result_available_at"), encode(match)))
        return len(matches)

    def matches(self, league: str | None = None) -> list[dict]:
        query = "SELECT payload FROM matches"
        params: tuple = ()
        if league:
            query += " WHERE league=?"
            params = (league,)
        query += " ORDER BY kickoff,id"
        with self.connect() as db:
            return [json.loads(row[0]) for row in db.execute(query, params)]

    def match(self, match_id: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT payload FROM matches WHERE id=?", (match_id,)).fetchone()
            return json.loads(row[0]) if row else None

    def save_prediction(self, match_id: str, prediction: dict) -> bool:
        with self.connect() as db:
            cursor = db.execute("""INSERT OR IGNORE INTO predictions
                (id,match_id,created_at,as_of,model_version,historical,payload) VALUES(?,?,?,?,?,?,?)""",
                (prediction["id"], match_id, prediction["created_at"], prediction["as_of"],
                 prediction["model_version"], int(prediction["historical"]), encode(prediction)))
            return cursor.rowcount == 1

    def predictions(self, match_id: str, historical: bool | None = None) -> list[dict]:
        query = "SELECT payload FROM predictions WHERE match_id=?"
        params: tuple = (match_id,)
        if historical is not None:
            query += " AND historical=?"
            params += (int(historical),)
        query += " ORDER BY as_of DESC,created_at DESC"
        with self.connect() as db:
            return [json.loads(row[0]) for row in db.execute(query, params)]

    def prediction_map(self) -> dict[str, dict]:
        with self.connect() as db:
            rows = db.execute("SELECT match_id,payload FROM predictions WHERE historical=0 ORDER BY as_of,created_at")
            return {row[0]: json.loads(row[1]) for row in rows}

    def history(self, league: str | None = None, limit: int = 100, offset: int = 0) -> dict:
        where = " WHERE m.league=?" if league else ""
        params: tuple = (league,) if league else ()
        join = " FROM predictions p JOIN matches m ON m.id=p.match_id LEFT JOIN settlements s ON s.prediction_id=p.id"
        with self.connect() as db:
            total = db.execute("SELECT COUNT(*)" + join + where, params).fetchone()[0]
            rows = db.execute("SELECT p.payload,m.payload,s.actual,s.outcome,s.settled_at" + join + where +
                              " ORDER BY p.as_of DESC,p.created_at DESC LIMIT ? OFFSET ?", params + (limit, offset))
            predictions = []
            for row in rows:
                item = json.loads(row[0])
                item.update(match=json.loads(row[1]), actual=json.loads(row[2]) if row[2] else None,
                            outcome=json.loads(row[3]) if row[3] else None, settled_at=row[4])
                latest, original = item["match"], item["actual"]
                item["actual_correction"] = ({"home_goals": latest["home_goals"], "away_goals": latest["away_goals"],
                                              "source": latest["source"], "observed_at": latest["fetched_at"]}
                                             if original and any(original.get(k) != latest.get(k) for k in ("home_goals", "away_goals")) else None)
                predictions.append(item)
            return {"predictions": predictions, "total": total, "limit": limit, "offset": offset}

    def save_report(self, league: str, created_at: str, report: dict) -> None:
        with self.connect() as db:
            db.execute("INSERT INTO reports(league,created_at,payload) VALUES(?,?,?)", (league, created_at, encode(report)))

    def latest_reports(self, league: str | None = None) -> list[dict]:
        with self.connect() as db:
            query = "SELECT league,payload,created_at FROM reports WHERE id IN (SELECT MAX(id) FROM reports GROUP BY league)"
            params: tuple = ()
            if league:
                query += " AND league=?"
                params = (league,)
            return [{**json.loads(row[1]), "league": row[0], "created_at": row[2]} for row in db.execute(query, params)]

    def put_status(self, name: str, payload: dict) -> None:
        with self.connect() as db:
            db.execute("INSERT INTO source_status(name,payload) VALUES(?,?) ON CONFLICT(name) DO UPDATE SET payload=excluded.payload",
                       (name, encode(payload)))

    def source_status(self) -> list[dict]:
        with self.connect() as db:
            return [json.loads(row[0]) for row in db.execute("SELECT payload FROM source_status ORDER BY name")]

    def put_job(self, job: dict) -> None:
        with self.connect() as db:
            db.execute("""INSERT INTO jobs(id,kind,status,created_at,started_at,completed_at,payload)
                VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET status=excluded.status,
                started_at=excluded.started_at,completed_at=excluded.completed_at,payload=excluded.payload""",
                (job["id"], job["kind"], job["status"], job["created_at"], job.get("started_at"),
                 job.get("completed_at"), encode(job)))

    def job(self, job_id: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT payload FROM jobs WHERE id=?", (job_id,)).fetchone()
            return json.loads(row[0]) if row else None

    def jobs(self, limit: int = 20) -> list[dict]:
        with self.connect() as db:
            return [json.loads(row[0]) for row in db.execute("SELECT payload FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,))]

    def metadata(self, key: str, default: Any = None) -> Any:
        with self.connect() as db:
            row = db.execute("SELECT payload FROM metadata WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else default

    def put_metadata(self, key: str, value: Any) -> None:
        with self.connect() as db:
            db.execute("INSERT INTO metadata(key,payload) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET payload=excluded.payload",
                       (key, encode(value)))

    def readiness(self) -> bool:
        with self.connect() as db:
            return db.execute("SELECT 1").fetchone()[0] == 1

    def save_manual_analysis(self, analysis: dict) -> bool:
        """Manual inputs never enter provider matches, training or measured performance."""
        with self.connect() as db:
            cursor = db.execute("INSERT OR IGNORE INTO manual_analyses(id,created_at,mode,payload) VALUES(?,?,?,?)",
                                (analysis["id"], analysis["created_at"], analysis["mode"], encode(analysis)))
            return cursor.rowcount == 1

    def manual_analysis(self, analysis_id: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT payload FROM manual_analyses WHERE id=?", (analysis_id,)).fetchone()
            return json.loads(row[0]) if row else None

    def manual_analyses(self, limit: int = 20, offset: int = 0) -> dict:
        with self.connect() as db:
            total = db.execute("SELECT COUNT(*) FROM manual_analyses").fetchone()[0]
            rows = db.execute("SELECT payload FROM manual_analyses ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?", (limit, offset))
            items = []
            for row in rows:
                value = json.loads(row[0])
                match, prediction = value["match"], value["prediction"]
                items.append({"id": value["id"], "created_at": value["created_at"], "mode": value["mode"],
                              **{key: match[key] for key in ("league", "home", "away", "kickoff")},
                              "probabilities": prediction["probabilities"], "expected_goals": prediction["expected_goals"]})
            return {"analyses": items, "total": total, "limit": limit, "offset": offset}
