"""Real-data synchronization, pre-match prediction and auditable background jobs."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import statistics
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .database import Database, ROOT

LEAGUE_NAMES = {"PL": "英格蘭超級聯賽", "PD": "西班牙甲組聯賽", "BL1": "德國甲組聯賽", "SA": "意大利甲組聯賽", "FL1": "法國甲組聯賽"}
DEFAULT_SEASONS = ["2022-23", "2023-24", "2024-25", "2025-26", "2026-27"]


def default_seasons() -> list[str]:
    stamp = datetime.now(timezone.utc)
    current = stamp.year if stamp.month >= 7 else stamp.year - 1
    return [f"{year}-{str(year+1)[2:]}" for year in range(max(2022, current - 14), current + 1)]


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def parsed(value: str) -> datetime:
    value = value.replace("Z", "+00:00")
    result = datetime.fromisoformat(value)
    return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result.astimezone(timezone.utc)


def public_error(exc: BaseException) -> str:
    """Never expose provider credentials or arbitrary library exception content."""
    return f"{type(exc).__name__}：工作未能完成。請檢查數據來源狀態及伺服器設定。"


def known_history(matches: list[dict], cutoff: str) -> list[dict]:
    instant = parsed(cutoff)
    return [m for m in matches if m.get("status") == "FINISHED" and
            m.get("home_goals") is not None and m.get("away_goals") is not None and
            m.get("result_available_at") and parsed(m["result_available_at"]) < instant and
            parsed(m["kickoff"]) < instant]


class FootballService:
    def __init__(self, database: Database):
        self.db = database
        self._lock = asyncio.Lock()
        self._tasks: set[asyncio.Task] = set()
        self._scheduler_task: asyncio.Task | None = None
        self.scheduler_enabled = False
        self._engines: dict[tuple, Any] = {}

    def recover_jobs(self) -> None:
        for job in self.db.jobs(10000):
            if job["status"] in {"pending", "running"}:
                job.update(status="failed", completed_at=now(), error="伺服器重啟中止了工作；可重新執行。")
                self.db.put_job(job)

    def current_config(self, league: str) -> dict | None:
        reports = self.db.latest_reports(league)
        if reports and reports[0].get("inference_config", {}).get("validated"):
            return reports[0]["inference_config"]
        return None

    def schedule_next(self, kind: str, delay: timedelta) -> None:
        schedule = self.db.metadata("schedule", {})
        schedule[kind] = (datetime.now(timezone.utc) + delay).isoformat()
        self.db.put_metadata("schedule", schedule)

    def training_signature(self, league: str) -> str:
        rows = known_history(self.db.matches(league), now())
        inputs = [(m["id"], m["home_goals"], m["away_goals"], m["result_available_at"]) for m in rows]
        return hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()

    def engine(self, league: str, cutoff: str):
        from .models import ModelEngine
        history = known_history(self.db.matches(league), cutoff)
        config = self.current_config(league)
        if config and config.get("trained_until") and parsed(config["trained_until"]) >= parsed(cutoff):
            config = None
        # Reuse fitted models only for the identical cutoff and input/config version.
        digest = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()[:12]
        key = (league, cutoff, len(history), digest)
        if key not in self._engines:
            self._engines[key] = ModelEngine(history, cutoff, config=config)
            while len(self._engines) > 20:
                self._engines.pop(next(iter(self._engines)))
        return self._engines[key], history

    def data_quality(self, match: dict, history: list[dict], cutoff: str, diagnostics: dict | None = None) -> dict:
        home_n = sum(m["home"] == match["home"] or m["away"] == match["home"] for m in history)
        away_n = sum(m["home"] == match["away"] or m["away"] == match["away"] for m in history)
        latest = max((m["kickoff"] for m in history), default=None)
        age_days = max(0, (parsed(cutoff) - parsed(latest)).days) if latest else None
        available = ["公開比賽賽果", "球隊歷史入球及失球", "Elo 球隊評分", "賽程日期"]
        missing = ["真實 xG／xGA", "已確認傷停", "已確認正選陣容"]
        if match.get("kickoff_precision") != "exact":
            missing.append("已確認開賽時間")
        if age_days is not None and age_days > 7:
            missing.append(f"近期完整賽果：最新賽果已相隔 {age_days} 日")
        # Source completeness is separate from highest outcome probability.
        sample_quality = min(1.0, min(home_n, away_n) / 30)
        freshness = max(0.1, 1 - max(0, (age_days or 0) - 7) / 35)
        score = round(100 * sample_quality * freshness * (0.75 if match.get("kickoff_precision") != "exact" else 0.85), 1)
        return {"score": score, "available": available, "missing": missing, "history_count": len(history),
                "home_samples": home_n, "away_samples": away_n, "latest_result_date": latest,
                "result_age_days": age_days, "source": match.get("source"), "as_of": cutoff,
                "retrospective_availability_limitation": "公開歷史檔案無法證明每項賽果當時的發布時間；回測採用保守可用時間。"}

    def make_prediction(self, match: dict, cutoff: str, raw: dict, history: list[dict], historical: bool = False) -> dict:
        from .models import markets_from_matrix
        matrix = raw["matrix"]
        total = sum(sum(row) for row in matrix)
        if not matrix or not math.isfinite(total) or abs(total - 1) > 1e-6 or any(v < 0 or not math.isfinite(v) for r in matrix for v in r):
            raise ValueError("Invalid score distribution")
        probabilities = {"home": sum(v for i, row in enumerate(matrix) for j, v in enumerate(row) if i > j),
                         "draw": sum(v for i, row in enumerate(matrix) for j, v in enumerate(row) if i == j),
                         "away": sum(v for i, row in enumerate(matrix) for j, v in enumerate(row) if i < j)}
        expected_goals = {"home": sum(i*v for i,row in enumerate(matrix) for v in row),
                          "away": sum(j*v for row in matrix for j,v in enumerate(row))}
        if abs(sum(probabilities[k] for k in ("home", "draw", "away")) - 1) > 1e-6:
            raise ValueError("Invalid outcome probabilities")
        quality = self.data_quality(match, history, cutoff, raw.get("diagnostics"))
        config = self.current_config(match["league"])
        if config and config.get("trained_until") and parsed(config["trained_until"]) >= parsed(cutoff):
            config = None
        calibrated = bool(config and config.get("validated"))
        ece = float((config or {}).get("calibration_metrics", {}).get("ece", 0.25) or 0.25)
        model_vectors = []
        for value in raw.get("models", {}).values():
            if isinstance(value, dict):
                candidate = value.get("probabilities", value)
                if all(k in candidate for k in ("home", "draw", "away")):
                    model_vectors.append([float(candidate[k]) for k in ("home", "draw", "away")])
        disagreement = sum(statistics.pstdev(v[i] for v in model_vectors) for i in range(3)) / 3 if len(model_vectors) > 1 else 0.1
        score = round(max(0, min(100, quality["score"] * (max(0.4, 1 - ece) if calibrated else 0.55) * max(0.4, 1 - disagreement * 3))), 1)
        reasons = [f"歷史樣本：主隊 {quality['home_samples']} 場、客隊 {quality['away_samples']} 場",
                   "已完成按時間分隔的校準及樣本外測試" if calibrated else "尚未有獨立回測證明校準可靠，保守使用入球模型",
                   f"模型分歧程度 {disagreement:.3f}"]
        if quality.get("result_age_days", 0) and quality["result_age_days"] > 7:
            reasons.append(f"公開賽果資料已落後 {quality['result_age_days']} 日，預測可靠性受限制")
        version = raw.get("model_version", "football-intelligence-v1")
        if config:
            version += "-" + hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()[:10]
        snapshot_id = hashlib.sha256(f"{match['id']}|{cutoff}|{version}|{int(historical)}".encode()).hexdigest()[:32]
        return {"id": snapshot_id, "match_id": match["id"], "created_at": now(), "as_of": cutoff,
                "model_version": version, "expected_goals": expected_goals, "probabilities": probabilities,
                "match_snapshot": dict(match),
                "markets": markets_from_matrix(matrix), "confidence": {"label": "較高" if score >= 70 else "中等" if score >= 45 else "偏低", "score": score, "reasons": reasons},
                "data_quality": quality, "matrix": matrix, "models": raw.get("models", {}),
                "diagnostics": raw.get("diagnostics", {}), "source": "walk-forward-backtest" if historical else match["source"],
                "historical": historical, "expected_goals_label": "模型估計入球（並非實測 xG）"}

    async def sync(self, provider: str = "openfootball", seasons: list[str] | None = None, leagues: list[str] | None = None) -> dict:
        from .providers import sync_football_data, sync_openfootball
        leagues = leagues or list(LEAGUE_NAMES)
        if provider == "football-data.org":
            key = os.environ.get("FOOTBALL_DATA_API_KEY", "")
            if not key:
                raise ValueError("Football Data API key is not configured")
            if os.environ.get("FOOTBALL_DATA_TERMS_ACCEPTED", "").lower() != "true":
                raise ValueError("Football Data terms must be accepted before use")
            result = await sync_football_data(key, leagues)
        elif provider == "openfootball":
            result = await sync_openfootball(seasons or default_seasons(), leagues)
        else:
            raise ValueError("Unsupported provider")
        rows = result.get("matches", [])
        count = self.db.ingest(rows)
        errors = result.get("errors", [])
        # Provider messages may include source URLs but never request headers or keys.
        secrets = [v for k, v in os.environ.items() if k in {"FOOTBALL_DATA_API_KEY", "ADMIN_TOKEN"} and v]
        safe_errors = []
        for value in errors[:100]:
            message = str(value)
            for secret in secrets:
                message = message.replace(secret, "[redacted]")
            safe_errors.append(message[:1000])
        stamp = now()
        latest = {league: max((m["kickoff"] for m in self.db.matches(league) if m["status"] == "FINISHED"), default=None) for league in leagues}
        warnings = [f"{LEAGUE_NAMES.get(l, l)}：最新公開賽果距今 {(parsed(stamp)-parsed(d)).days} 日，未能證實最近賽事資料完整。"
                    for l, d in latest.items() if d and (parsed(stamp)-parsed(d)).days > 7]
        source = {"name": provider, "last_sync": stamp, "status": "partial" if errors else "ok", "ingested": count,
                  "errors": safe_errors, "warnings": warnings, "latest_result_dates": latest,
                  "sources": result.get("sources", []), "next_retry_at": (parsed(stamp) + timedelta(hours=1)).isoformat() if errors else None}
        self.db.put_status(provider, source)
        self.schedule_next("sync", timedelta(hours=1))
        self._engines.clear()
        settled = self.settle()
        return {"provider": provider, "ingested": count, "errors": safe_errors, "warnings": warnings, "settled": settled}

    def predict_upcoming(self, final_only: bool = False) -> dict:
        cutoff = now()
        matches = self.db.matches()
        engines = {}
        predicted, skipped, errors = 0, [], []
        end = parsed(cutoff) + timedelta(days=8)
        for match in matches:
            kickoff = parsed(match["kickoff"])
            if match["status"] != "SCHEDULED" or kickoff <= parsed(cutoff) or kickoff > end:
                continue
            is_final = match.get("kickoff_precision") == "exact" and kickoff - parsed(cutoff) <= timedelta(minutes=15)
            if final_only and not is_final:
                continue
            if is_final:
                snapshots = self.db.predictions(match["id"], historical=False)
                if snapshots and snapshots[0].get("final_snapshot") and snapshots[0].get("match_snapshot", {}).get("kickoff") == match["kickoff"]:
                    continue
            if match.get("kickoff_precision") == "date" and kickoff.date() <= parsed(cutoff).astimezone(ZoneInfo("Asia/Hong_Kong")).date():
                skipped.append({"match_id": match["id"], "reason": "賽程只有日期；當日是否已開賽未能確認，因此不產生賽前預測。"})
                continue
            league = match["league"]
            if league not in engines:
                engines[league] = self.engine(league, cutoff)
            engine, history = engines[league]
            quality = self.data_quality(match, history, cutoff)
            if len(history) < 80 or min(quality["home_samples"], quality["away_samples"]) < 5:
                skipped.append({"match_id": match["id"], "reason": "歷史數據不足：至少需要聯賽 80 場及每隊 5 場已知賽果。"})
                continue
            try:
                raw = engine.predict(match["home"], match["away"], league, kickoff=match["kickoff"])
                prediction = self.make_prediction(match, cutoff, raw, history)
                prediction["final_snapshot"] = is_final
                prediction["final_snapshot_note"] = ("依目前已確認開賽時間，在開賽前 15 分鐘內保存。" if is_final else
                                                        "開賽時間尚未確認；不會聲稱為最後賽前版本。" if match.get("kickoff_precision") != "exact" else "定期更新的賽前版本。")
                predicted += int(self.db.save_prediction(match["id"], prediction))
            except Exception as exc:
                errors.append({"match_id": match["id"], "error": public_error(exc)})
        if not final_only:
            self.schedule_next("predict", timedelta(days=1))
        return {"created": predicted, "skipped": skipped, "errors": errors, "as_of": cutoff, "final_only": final_only}

    def train(self, league: str = "all") -> dict:
        from .backtest import run_backtest
        leagues = list(LEAGUE_NAMES) if league == "all" else [league]
        outputs = []
        for code in leagues:
            matches = self.db.matches(code)
            report = run_backtest(matches, code)
            stamp = now()
            rows = report.pop("prediction_rows", [])
            self.db.save_report(code, stamp, report)
            if report.get("status") == "completed":
                self.db.put_metadata("trained_signature:" + code, self.training_signature(code))
            known = {m["id"]: m for m in matches}
            saved = 0
            for row in rows:
                match = known.get(row.get("match_id"))
                if not match:
                    continue
                matrix = row["matrix"]
                raw = {**row, "models": row.get("model_probabilities", {}),
                       "diagnostics": {"fit_as_of": row.get("fit_as_of"), "known_results": row.get("known_results")},
                       "expected_goals": row.get("expected_goals", {
                    "home": sum(i * value for i, cells in enumerate(matrix) for value in cells),
                    "away": sum(j * value for cells in matrix for j, value in enumerate(cells))})}
                history = known_history(matches, row["as_of"])
                prediction = self.make_prediction(match, row["as_of"], raw, history, historical=True)
                saved += int(self.db.save_prediction(match["id"], prediction))
            outputs.append({"league": code, "sample_count": report.get("sample_count"), "status": report.get("status", "complete"),
                            "snapshots_saved": saved, "selected_model": report.get("selected_model")})
        self._engines.clear()
        self.schedule_next("train", timedelta(days=7))
        self.settle()
        return {"reports": outputs}

    def settle(self) -> int:
        settled = 0
        with self.db.connect() as db:
            rows = list(db.execute("SELECT p.id,p.payload,m.payload FROM predictions p JOIN matches m ON m.id=p.match_id LEFT JOIN settlements s ON s.prediction_id=p.id WHERE s.prediction_id IS NULL AND m.status='FINISHED'"))
            for row in rows:
                prediction, match = json.loads(row[1]), json.loads(row[2])
                if match.get("home_goals") is None or match.get("away_goals") is None or not match.get("result_available_at") or parsed(match["result_available_at"]) > parsed(now()):
                    continue
                home, away = match["home_goals"], match["away_goals"]
                outcome = "home" if home > away else "away" if away > home else "draw"
                probabilities = prediction["probabilities"]
                observed = {"home_goals": home, "away_goals": away, "outcome": outcome, "source": match["source"], "source_url": match.get("source_url"), "result_available_at": match["result_available_at"]}
                metrics = {"correct": max(probabilities, key=probabilities.get) == outcome,
                           "log_loss": -math.log(max(1e-15, probabilities[outcome])),
                           "brier": sum((probabilities[k] - int(k == outcome)) ** 2 for k in ("home", "draw", "away"))}
                cursor = db.execute("INSERT OR IGNORE INTO settlements(prediction_id,settled_at,actual,outcome) VALUES(?,?,?,?)",
                                    (row[0], now(), json.dumps(observed), json.dumps(metrics)))
                settled += cursor.rowcount
        return settled

    def monitoring(self, league: str | None = None) -> dict:
        # Last genuinely saved pre-match snapshot per match; never mix backtests into live monitoring.
        with self.db.connect() as db:
            query = """SELECT p.match_id,p.as_of,s.outcome FROM predictions p
                JOIN settlements s ON s.prediction_id=p.id JOIN matches m ON m.id=p.match_id
                WHERE p.historical=0"""
            params: tuple = ()
            if league:
                query += " AND m.league=?"
                params = (league,)
            query += " ORDER BY m.kickoff,p.as_of"
            records = {row[0]: json.loads(row[2]) for row in db.execute(query, params)}
        values = list(records.values())
        count = len(values)
        return {"sample_count": count, "mode": "prospective", "accuracy": sum(v["correct"] for v in values) / count if count else None,
                "log_loss": sum(v["log_loss"] for v in values) / count if count else None,
                "brier": sum(v["brier"] for v in values) / count if count else None,
                "status": "insufficient_data" if count < 100 else "monitoring",
                "message": "只計算真正賽前保存的最終版本；需要至少 100 場已完賽預測才評估表現退化。",
                "drift_alert": None if count < 100 else self._drift(values)}

    @staticmethod
    def _drift(values: list[dict]) -> bool:
        if len(values) < 100:
            return False
        recent = sum(v["log_loss"] for v in values[-50:]) / 50
        earlier = sum(v["log_loss"] for v in values[:-50]) / (len(values) - 50)
        return recent > earlier * 1.15

    def comparison(self, match: dict, cutoff: str) -> dict:
        engine, _ = self.engine(match["league"], cutoff)
        result = {side: self.team_summary(engine, match[side], match["league"], cutoff) for side in ("home", "away")}
        for value in result.values():
            value.update(xg=None, xga=None, injuries=None, lineup=None,
                         source_urls=sorted({m.get("source_url", "") for m in known_history(self.db.matches(match["league"]), cutoff) if m["home"] == value["name"] or m["away"] == value["name"]}),
                         note="估計入球與攻守強度為模型輸出；沒有真實 xG、傷停或正選數據。")
            value["rest_days_to_match"] = (parsed(match["kickoff"]) - parsed(value["last_match_at"])).days if value.get("last_match_at") else None
            value["rest_days_note"] = "只按已收錄的上次賽果計算；缺少近期資料可令休息日被高估。"
        return result

    def team_summary(self, engine, team: str, league: str, cutoff: str) -> dict:
        summary = engine.team_summary(team, league)
        history = [m for m in engine.known if m["league"] == league and team in (m["home"], m["away"])]
        def aggregate(rows: list[dict]) -> dict:
            n = len(rows)
            gf = sum(m["home_goals"] if m["home"] == team else m["away_goals"] for m in rows)
            ga = sum(m["away_goals"] if m["home"] == team else m["home_goals"] for m in rows)
            wins = sum((m["home_goals"] > m["away_goals"]) if m["home"] == team else (m["away_goals"] > m["home_goals"]) for m in rows)
            draws = sum(m["home_goals"] == m["away_goals"] for m in rows)
            return {"matches": n, "wins": wins, "draws": draws, "losses": n-wins-draws,
                    "goals_for_per_game": gf/n if n else None, "goals_against_per_game": ga/n if n else None,
                    "win_rate": wins/n if n else None, "points_per_game": (wins*3+draws)/n if n else None}
        summary.update(aggregate(history))
        summary.update(home=aggregate([m for m in history if m["home"] == team]),
                       away=aggregate([m for m in history if m["away"] == team]),
                       last_match_at=max((m["kickoff"] for m in history), default=None),
                       period={"start": min((m["kickoff"] for m in history), default=None), "end": max((m["kickoff"] for m in history), default=None)},
                       statistic_scope="所有已收錄且在統計截點前可用的歷史賽果，並非只計今季",
                       schedule_games_14_days=sum(0 <= (parsed(cutoff)-parsed(m["kickoff"])).days < 14 for m in history))
        return summary

    def analysis_report(self, match: dict, prediction: dict | None, comparison: dict) -> list[dict]:
        if not prediction:
            return [{"title": "尚未有可用預測", "text": "此賽事沒有已保存的賽前模型輸出；可能缺少歷史數據、開賽時間不明，或比賽已開始。"}]
        h, a = comparison["home"], comparison["away"]
        probabilities = prediction["probabilities"]
        favourite = max(probabilities, key=probabilities.get)
        result_name = {"home": "主隊勝出", "draw": "和局", "away": "客隊勝出"}[favourite]
        goals = prediction["expected_goals"]
        return [
            {"title": "兩隊實力比較", "text": f"以預測時間前已知賽果計算，{match['home']} 的 Elo 為 {h.get('elo', 1500):.0f}，{match['away']} 為 {a.get('elo', 1500):.0f}。Elo 為模型評分。"},
            {"title": "主客場因素", "text": "入球模型會從歷史賽果估計聯賽主場效應及兩隊攻守強度；未有可靠證據確認場地變更。"},
            {"title": "進攻與防守對位", "text": f"模型估計主隊 {goals['home']:.2f} 球、客隊 {goals['away']:.2f} 球。這些是模型估計入球，並非實測 xG。"},
            {"title": "近期狀態及對手質素", "text": "近期狀態只採用預測時間前已可取得的賽果；Elo 按對手實力更新。可在球隊比較查看最近 5 場及 10 場。"},
            {"title": "傷停及正選陣容", "text": "數據不足：目前來源沒有可核實的傷停及正選名單；模型未使用這些資訊。"},
            {"title": "預計入球情況", "text": f"比分分布預期合共 {goals['home'] + goals['away']:.2f} 球；大於 2.5 球及雙方入球概率均由同一比分矩陣推算，不能據此確定實際節奏。"},
            {"title": "最可能結果", "text": f"模型概率最高的結果為{result_name}，概率 {probabilities[favourite]*100:.1f}%。概率最高不代表保證勝出。"},
            {"title": "不確定因素", "text": "；".join(prediction["data_quality"]["missing"] + prediction["confidence"]["reasons"])},
        ]

    async def queue_job(self, kind: str, params: dict | None = None) -> dict:
        params = params or {}
        for existing in self.db.jobs(100):
            if existing["kind"] == kind and existing["status"] in {"pending", "running"} and existing.get("params") == params:
                return {"job_id": existing["id"], "status": existing["status"]}
        job = {"id": uuid.uuid4().hex, "kind": kind, "status": "pending", "created_at": now(), "params": params}
        self.db.put_job(job)
        task = asyncio.create_task(self._run_job(job))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return {"job_id": job["id"], "status": "pending"}

    async def _run_job(self, job: dict) -> None:
        async with self._lock:
            job.update(status="running", started_at=now())
            self.db.put_job(job)
            try:
                if job["kind"] == "sync":
                    result = await self.sync(**job["params"])
                    # Every successful data refresh also creates a fresh version for future matches.
                    result["predictions"] = await asyncio.to_thread(self.predict_upcoming)
                elif job["kind"] == "train":
                    result = await asyncio.to_thread(self.train, **job["params"])
                    result["predictions"] = await asyncio.to_thread(self.predict_upcoming)
                elif job["kind"] == "predict":
                    result = await asyncio.to_thread(self.predict_upcoming, **job["params"])
                else:
                    raise ValueError("Unknown job")
                job.update(status="completed", result=result)
            except asyncio.CancelledError:
                job.update(status="failed", error="伺服器關閉中止了工作；可重新執行。")
                raise
            except Exception as exc:
                job.update(status="failed", error=public_error(exc), next_retry_at=(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat())
                self.schedule_next(job["kind"], timedelta(hours=1))
            finally:
                job["completed_at"] = now()
                self.db.put_job(job)

    async def start_scheduler(self) -> None:
        # A CLI bootstrap may have been performed before this process starts.
        # Infer only missing timer entries from persisted completed work.
        schedule = self.db.metadata("schedule", {})
        sources = self.db.source_status()
        if "sync" not in schedule and sources:
            latest_sync = max((s["last_sync"] for s in sources if s.get("last_sync")), default=None)
            if latest_sync:
                schedule["sync"] = (parsed(latest_sync) + timedelta(hours=1)).isoformat()
        reports = [r for r in self.db.latest_reports() if r.get("status") == "completed"]
        if "train" not in schedule and reports:
            schedule["train"] = (max(parsed(r["created_at"]) for r in reports) + timedelta(days=7)).isoformat()
        with self.db.connect() as connection:
            latest_prediction = connection.execute("SELECT MAX(created_at) FROM predictions WHERE historical=0").fetchone()[0]
        if "predict" not in schedule and latest_prediction:
            schedule["predict"] = (parsed(latest_prediction) + timedelta(days=1)).isoformat()
        self.db.put_metadata("schedule", schedule)
        self.scheduler_enabled = True
        self._scheduler_task = asyncio.create_task(self._scheduler_loop())

    async def _scheduler_loop(self) -> None:
        while True:
            stamp = datetime.now(timezone.utc)
            schedule = self.db.metadata("schedule", {})
            intervals = {"sync": timedelta(hours=1), "predict": timedelta(days=1), "train": timedelta(days=7)}
            for kind, interval in intervals.items():
                due = not schedule.get(kind) or stamp >= parsed(schedule[kind])
                if not due:
                    continue
                # Retraining requires sufficient genuine results; no empty model artifacts.
                if kind == "train" and sum(m["status"] == "FINISHED" for m in self.db.matches()) < 500:
                    continue
                if kind == "train" and all(self.db.metadata("trained_signature:" + code) == self.training_signature(code) for code in LEAGUE_NAMES):
                    schedule[kind] = (stamp + interval).isoformat()
                    self.db.put_metadata("schedule", schedule)
                    continue
                params = {"provider": os.environ.get("DATA_PROVIDER", "openfootball")} if kind == "sync" else {"league": "all"} if kind == "train" else {}
                await self.queue_job(kind, params)
                schedule[kind] = (stamp + interval).isoformat()
                self.db.put_metadata("schedule", schedule)
            if any(m["status"] == "SCHEDULED" and m.get("kickoff_precision") == "exact" and
                   stamp < parsed(m["kickoff"]) <= stamp + timedelta(minutes=15) and
                   not any(p.get("final_snapshot") and p.get("match_snapshot", {}).get("kickoff") == m["kickoff"] for p in self.db.predictions(m["id"], historical=False))
                   for m in self.db.matches()):
                await self.queue_job("predict", {"final_only": True})
            await asyncio.sleep(30)

    async def shutdown(self) -> None:
        self.scheduler_enabled = False
        if self._scheduler_task:
            self._scheduler_task.cancel()
            await asyncio.gather(self._scheduler_task, return_exceptions=True)
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)


async def cli() -> None:
    parser = argparse.ArgumentParser(description="Football Intelligence AI: actual-data administration")
    parser.add_argument("command", choices=["sync", "train", "predict", "bootstrap", "settle"])
    parser.add_argument("--provider", default="openfootball", choices=["openfootball", "football-data.org"])
    parser.add_argument("--league", default="all", choices=["all", *LEAGUE_NAMES])
    parser.add_argument("--seasons", nargs="+")
    parser.add_argument("--leagues", nargs="+", choices=list(LEAGUE_NAMES))
    args = parser.parse_args()
    service = FootballService(Database())
    outputs = {}
    if args.command in {"sync", "bootstrap"}:
        outputs["sync"] = await service.sync(args.provider, args.seasons, args.leagues)
    if args.command in {"train", "bootstrap"}:
        outputs["train"] = await asyncio.to_thread(service.train, args.league)
    if args.command in {"predict", "bootstrap"}:
        outputs["predict"] = await asyncio.to_thread(service.predict_upcoming)
    if args.command == "settle":
        outputs["settled"] = service.settle()
    print(json.dumps(outputs, ensure_ascii=False, allow_nan=False))


if __name__ == "__main__":
    asyncio.run(cli())
