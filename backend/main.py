"""Single-origin FastAPI application serving the real-data API and React UI."""
from __future__ import annotations

import hmac
import ipaddress
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from .database import Database, ROOT
from .service import FootballService, LEAGUE_NAMES, now, parsed

HK = ZoneInfo("Asia/Hong_Kong")


class SyncRequest(BaseModel):
    provider: Literal["openfootball", "football-data.org"] = "openfootball"


class TrainRequest(BaseModel):
    league: Literal["all", "PL", "PD", "BL1", "SA", "FL1"] = "all"


def is_loopback(host: str | None) -> bool:
    try:
        return bool(host and ipaddress.ip_address(host).is_loopback)
    except ValueError:
        return host == "localhost"


def require_admin(request: Request) -> None:
    """Unproxied peer addresses only. Secrets never pass through URLs or cookies."""
    origin = request.headers.get("origin")
    referer = request.headers.get("referer")
    token = os.environ.get("ADMIN_TOKEN", "")
    supplied = request.headers.get("x-admin-token", "")
    authenticated = bool(token and supplied and hmac.compare_digest(token.encode(), supplied.encode()))
    for address in (origin, referer):
        if address:
            parsed_address = urlsplit(address)
            proxy_https = authenticated and parsed_address.scheme == "https" and request.url.scheme == "http"
            if (parsed_address.scheme != request.url.scheme and not proxy_https) or parsed_address.netloc != request.url.netloc:
                raise HTTPException(403, "管理操作只接受同一網站的請求。")
    if token:
        if not authenticated:
            raise HTTPException(401, "需要有效的管理權限。")
        return
    peer = request.client.host if request.client else None
    if not is_loopback(peer) or not is_loopback(request.url.hostname):
        raise HTTPException(403, "未設定管理密碼；管理操作只可在本機使用。請於環境設定加入 ADMIN_TOKEN。")


def create_app(db_path: str | Path | None = None, scheduler: bool | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(application: FastAPI):
        application.state.service.recover_jobs()
        enabled = scheduler if scheduler is not None else os.environ.get("SCHEDULER_ENABLED", "true").lower() == "true"
        if enabled:
            await application.state.service.start_scheduler()
        yield
        await application.state.service.shutdown()

    application = FastAPI(title="Football Intelligence AI", version="1.0.0", lifespan=lifespan,
                          docs_url="/api/docs", redoc_url=None)
    application.state.service = FootballService(Database(db_path))

    @application.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' https: data:; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'self'"
        if request.url.path.startswith("/api"):
            response.headers["Cache-Control"] = "no-store"
        return response

    def service() -> FootballService:
        return application.state.service

    @application.get("/health")
    def health(s: FootballService = Depends(service)):
        try:
            healthy = s.db.readiness()
        except Exception:
            return JSONResponse({"status": "unhealthy", "database": "unavailable"}, status_code=503)
        return {"status": "ok" if healthy else "unhealthy", "database": "ready", "time": now()}

    @application.get("/api/status")
    def status(s: FootballService = Depends(service)):
        matches = s.db.matches()
        instant = datetime.now(timezone.utc)
        with s.db.connect() as db:
            predictions = db.execute("SELECT COUNT(*) FROM predictions WHERE historical=0").fetchone()[0]
            retrospective = db.execute("SELECT COUNT(*) FROM predictions WHERE historical=1").fetchone()[0]
        sources = s.db.source_status()
        issues = [warning for source in sources for warning in source.get("warnings", [])]
        if not matches:
            issues.append("尚未同步真實足球數據；請在管理設定啟動公開數據同步。")
        if any(m.get("kickoff_precision") == "date" and m["status"] == "SCHEDULED" for m in matches):
            issues.append("公開賽程只有日期，開賽時間未經核實；當日賽事不產生新預測。")
        last_result = max((m["kickoff"] for m in matches if m["status"] == "FINISHED"), default=None)
        result_age = (instant - parsed(last_result)).days if last_result else None
        return {"mode": "real" if matches else "empty", "counts": {"matches": len(matches),
                "finished": sum(m["status"] == "FINISHED" for m in matches),
                "upcoming": sum(m["status"] == "SCHEDULED" and parsed(m["kickoff"]) > instant for m in matches),
                "predictions": predictions, "historical_predictions": retrospective}, "sources": sources,
                "jobs": s.db.jobs(), "scheduler": {"enabled": s.scheduler_enabled, "intervals": {"sync": "每小時", "predict": "每日及數據更新後", "train": "每星期（數據足夠時）"}, "next_runs": s.db.metadata("schedule", {})},
                "capabilities": {"real_results": bool(matches), "real_xg": False, "confirmed_injuries": False,
                                 "confirmed_lineups": False, "live_predictions": False, "pre_match": True,
                                 "official_fixtures": any(m["source"] == "football-data.org" for m in matches)},
                "issues": issues, "freshness": {"last_result_at": last_result, "stale_days": result_age,
                                                  "stale": result_age is None or result_age > 7}, "updated_at": now()}

    def validate_league(value: str | None) -> str | None:
        if value and value not in LEAGUE_NAMES:
            raise HTTPException(422, "不支援的聯賽。")
        return value or None

    @application.get("/api/matches")
    def matches(league: str | None = None, date: str | None = None, q: str = "", sort: str = "kickoff",
                window: str = "7", limit: int = Query(250, ge=1, le=1000), offset: int = Query(0, ge=0),
                s: FootballService = Depends(service)):
        items = s.db.matches(validate_league(league))
        today = datetime.now(HK).date()
        if window != "all":
            try:
                days = int(window)
                if not 1 <= days <= 31:
                    raise ValueError()
            except ValueError:
                raise HTTPException(422, "日期範圍必須是 1 至 31 或 all。")
            last = today + timedelta(days=days)
            items = [m for m in items if m["status"] == "SCHEDULED" and today <= match_date(m) < last]
        if date:
            try:
                requested = datetime.strptime(date, "%Y-%m-%d").date()
            except ValueError:
                raise HTTPException(422, "日期格式應為 YYYY-MM-DD。")
            items = [m for m in items if match_date(m) == requested]
        if q:
            text = q.casefold().strip()
            items = [m for m in items if text in m["home"].casefold() or text in m["away"].casefold() or text in LEAGUE_NAMES[m["league"]]]
        predictions = s.db.prediction_map()
        for match in items:
            match["prediction"] = predictions.get(match["id"])
            match["league_name"] = LEAGUE_NAMES.get(match["league"], match["league"])
        if sort in {"confidence", "goals", "expected_goals"}:
            items.sort(key=lambda m: (m["prediction"]["confidence"]["score"] if sort == "confidence" else sum(m["prediction"]["expected_goals"].values())) if m["prediction"] else -1, reverse=True)
        elif sort in {"league"}:
            items.sort(key=lambda m: (m["league"], m["kickoff"]))
        else:
            items.sort(key=lambda m: m["kickoff"])
        return {"matches": items[offset:offset+limit], "total": len(items), "window": window, "date": date,
                "timezone": "Asia/Hong_Kong", "limit": limit, "offset": offset}

    @application.get("/api/matches/{match_id}")
    def match_detail(match_id: str, s: FootballService = Depends(service)):
        match = s.db.match(match_id)
        if not match:
            raise HTTPException(404, "找不到這場比賽。")
        versions = s.db.predictions(match_id)
        prospective = [v for v in versions if not v["historical"]]
        prediction = prospective[0] if prospective else versions[0] if versions else None
        cutoff = prediction["as_of"] if prediction else min(now(), match["kickoff"])
        comparison = s.comparison(match, cutoff)
        gaps = prediction["data_quality"]["missing"] if prediction else ["沒有可用的賽前預測", "真實 xG／xGA", "已確認傷停", "已確認正選陣容"]
        return {"match": match, "prediction": prediction, "versions": versions,
                "comparison": comparison, "report": s.analysis_report(match, prediction, comparison), "data_gaps": gaps}

    @application.get("/api/teams")
    def teams(league: str | None = None, q: str = "", s: FootballService = Depends(service)):
        codes = [validate_league(league)] if league else list(LEAGUE_NAMES)
        cutoff = datetime.now(timezone.utc).replace(second=0, microsecond=0).isoformat()
        results = []
        for code in codes:
            matches = s.db.matches(code)
            names = sorted({m[side] for m in matches for side in ("home", "away")})
            if not names:
                continue
            engine, _ = s.engine(code, cutoff)
            for name in names:
                if not q or q.casefold() in name.casefold():
                    result = s.team_summary(engine, name, code, cutoff)
                    result.update(league_name=LEAGUE_NAMES[code], xg=None, xga=None)
                    results.append(result)
        return {"teams": results, "total": len(results), "as_of": cutoff, "source": "openfootball 已知歷史賽果；Elo 及強度為模型估計"}

    @application.get("/api/teams/{name}")
    def team_detail(name: str, league: str | None = None, s: FootballService = Depends(service)):
        code = validate_league(league)
        items = s.db.matches(code)
        found = next((m for m in items if name in (m["home"], m["away"])), None)
        if not found:
            raise HTTPException(404, "找不到這支球隊。")
        cutoff = datetime.now(timezone.utc).replace(second=0, microsecond=0).isoformat()
        engine, history = s.engine(found["league"], cutoff)
        summary = s.team_summary(engine, name, found["league"], cutoff)
        return {"team": summary, "form5": summary.get("form5", []), "form10": summary.get("form10", []),
                "matches": [m for m in history if name in (m["home"], m["away"])][-30:][::-1],
                "xg": None, "xga": None, "injuries": None, "lineup": None,
                "data_gaps": ["真實 xG／xGA", "已確認傷停", "已確認正選陣容"], "as_of": cutoff}

    @application.get("/api/history")
    def history(league: str | None = None, limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0), s: FootballService = Depends(service)):
        return {**s.db.history(validate_league(league), limit, offset), "note": "historical=true 為事後研究回測，與真正賽前保存的預測分開標示。"}

    @application.get("/api/performance")
    def performance(league: str | None = None, s: FootballService = Depends(service)):
        code = validate_league(league)
        reports = s.db.latest_reports(code)
        for report in reports:
            for model in report.get("models", []):
                model["selected"] = model.get("name") == report.get("selected_model")
        return {"reports": reports, "monitoring": s.monitoring(code),
                "limitations": ["歷史數據為回顧下載；無法證明原始發布時間。", "日期不等於已核實開賽時間。", "歷史表現不保證未來準確度。"]}

    @application.get("/api/settings")
    def settings(s: FootballService = Depends(service)):
        return {"provider": os.environ.get("DATA_PROVIDER", "openfootball"), "api_key_configured": bool(os.environ.get("FOOTBALL_DATA_API_KEY")),
                "admin_configured": bool(os.environ.get("ADMIN_TOKEN")), "terms_accepted": os.environ.get("FOOTBALL_DATA_TERMS_ACCEPTED", "").lower() == "true",
                "sources": s.db.source_status(), "leagues": [{"code": k, "name": v} for k, v in LEAGUE_NAMES.items()],
                "limitations": ["公開 Openfootball 數據採用 CC0，沒有實測 xG、傷停或正選陣容。", "Football-data.org 須由帳戶持有人核實條款並配置密鑰；未經批准不使用付費服務。", "所有密鑰只保存在伺服器環境變數。"],
                "environment_variables": ["FOOTBALL_DATA_API_KEY", "FOOTBALL_DATA_TERMS_ACCEPTED", "ADMIN_TOKEN"],
                "scheduler": {"enabled": s.scheduler_enabled, "next_runs": s.db.metadata("schedule", {})}}

    @application.post("/api/admin/sync", dependencies=[Depends(require_admin)], status_code=202)
    async def sync(body: SyncRequest, s: FootballService = Depends(service)):
        if body.provider == "football-data.org":
            if not os.environ.get("FOOTBALL_DATA_API_KEY"):
                raise HTTPException(422, "請先在環境的秘密設定加入 FOOTBALL_DATA_API_KEY，切勿在聊天或網址輸入密鑰。")
            if os.environ.get("FOOTBALL_DATA_TERMS_ACCEPTED", "").lower() != "true":
                raise HTTPException(422, "請先核實帳戶授權條款，再設定 FOOTBALL_DATA_TERMS_ACCEPTED=true。")
        return await s.queue_job("sync", {"provider": body.provider})

    @application.post("/api/admin/train", dependencies=[Depends(require_admin)], status_code=202)
    async def train(body: TrainRequest, s: FootballService = Depends(service)):
        return await s.queue_job("train", {"league": body.league})

    @application.post("/api/admin/predict", dependencies=[Depends(require_admin)], status_code=202)
    async def predict(s: FootballService = Depends(service)):
        return await s.queue_job("predict")

    @application.get("/api/jobs/{job_id}")
    def job(job_id: str, s: FootballService = Depends(service)):
        value = s.db.job(job_id)
        if not value:
            raise HTTPException(404, "找不到工作紀錄。")
        return value

    from .manual import install_manual_routes
    install_manual_routes(application, service, require_admin)

    dist = ROOT / "web/dist"

    @application.get("/{path:path}", include_in_schema=False)
    def web(path: str):
        if path.startswith("api/") or path == "api":
            raise HTTPException(404, "找不到 API。")
        target = (dist / path).resolve()
        if target.is_relative_to(dist.resolve()) and target.is_file():
            return FileResponse(target)
        index = dist / "index.html"
        if index.exists() and "." not in Path(path).name:
            return FileResponse(index)
        if path == "" and not index.exists():
            return JSONResponse({"status": "frontend_not_built", "message": "前端建置尚未完成；真實 API 已啟動。", "api": "/api/status"}, status_code=503)
        raise HTTPException(404, "找不到頁面。")

    return application


def match_date(match: dict):
    instant = parsed(match["kickoff"])
    # A date-only sentinel is an original fixture date, not an announced time.
    return instant.date() if match.get("kickoff_precision") == "date" else instant.astimezone(HK).date()


app = create_app()
