"""Isolated, auditable manual analyses; user data never trains official models."""
from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator

from .service import FootballService, now, parsed

League = Literal["PL", "PD", "BL1", "SA", "FL1"]
Rate = Annotated[float, Field(ge=0, le=10, allow_inf_nan=False)]
Score = Annotated[StrictInt, Field(ge=0, le=30)]


def clean_name(value: str) -> str:
    value = value.strip()
    if not value or len(value) > 100 or any(ord(char) < 32 for char in value):
        raise ValueError("球隊名稱須為 1 至 100 個字，並且不可包含換行或控制字元。")
    return value


def time_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


class HistoricalMatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kickoff: AwareDatetime
    home: str
    away: str
    home_goals: Score
    away_goals: Score
    available_at: AwareDatetime | None = None

    @field_validator("home", "away")
    @classmethod
    def names(cls, value: str) -> str:
        return clean_name(value)

    @field_validator("kickoff", "available_at")
    @classmethod
    def sensible_date(cls, value: datetime | None) -> datetime | None:
        if value is not None and not 1900 <= value.year <= 2100:
            raise ValueError("日期須介乎 1900 至 2100 年。")
        return value

    @model_validator(mode="after")
    def check_match(self):
        if self.home.casefold() == self.away.casefold():
            raise ValueError("主隊及客隊不可相同。")
        if self.available_at is not None and self.available_at < self.kickoff + timedelta(minutes=90):
            raise ValueError("賽果可用時間至少須在開賽 90 分鐘後，不能當作開賽前已知。")
        return self


class ManualAnalysisRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["history", "custom_history", "scenario"]
    league: League
    home: str
    away: str
    kickoff: AwareDatetime
    notes: str = Field(default="", max_length=2000)
    source_note: str = Field(default="", max_length=200)
    historical_matches: list[HistoricalMatch] = Field(default_factory=list, max_length=1000)
    home_expected_goals: Rate | None = None
    away_expected_goals: Rate | None = None

    @field_validator("home", "away")
    @classmethod
    def names(cls, value: str) -> str:
        return clean_name(value)

    @field_validator("kickoff")
    @classmethod
    def sensible_date(cls, value: datetime) -> datetime:
        if not 1900 <= value.year <= 2100:
            raise ValueError("日期須介乎 1900 至 2100 年。")
        return value

    @field_validator("home_expected_goals", "away_expected_goals", mode="before")
    @classmethod
    def numeric_rate(cls, value: Any) -> Any:
        if isinstance(value, bool):
            raise ValueError("估計入球須為 0 至 10 的有限數值。")
        return value

    @model_validator(mode="after")
    def check_mode(self):
        if self.home.casefold() == self.away.casefold():
            raise ValueError("主隊及客隊不可相同。")
        rates_present = self.home_expected_goals is not None or self.away_expected_goals is not None
        if self.mode == "scenario":
            if self.home_expected_goals is None or self.away_expected_goals is None:
                raise ValueError("自訂入球情境必須填寫主隊及客隊估計入球。")
            if self.historical_matches:
                raise ValueError("自訂入球情境不會訓練歷史賽果；請移除歷史資料或選擇手動賽果模式。")
        elif rates_present:
            raise ValueError("歷史模型模式不接受手動估計入球；如要使用入球假設，請選自訂入球情境。")
        if self.mode == "history" and self.historical_matches:
            raise ValueError("已收錄歷史模式不接受手動賽果；請選擇手動賽果模式。")
        if self.mode == "custom_history" and not self.historical_matches:
            raise ValueError("請輸入歷史賽果；至少需要合共 30 場，以及每支目標球隊 5 場。")
        return self


FIELD_NAMES = {
    "body": "輸入資料", "mode": "分析模式", "league": "聯賽", "home": "主隊", "away": "客隊",
    "kickoff": "開賽時間", "notes": "補充資料", "source_note": "資料來源說明",
    "historical_matches": "歷史賽果", "available_at": "賽果可用時間", "home_goals": "主隊入球",
    "away_goals": "客隊入球", "home_expected_goals": "主隊估計入球", "away_expected_goals": "客隊估計入球",
}


async def manual_validation_error(request: Request, exc: RequestValidationError):
    """Manual forms receive readable text, without reflecting untrusted input values."""
    if not request.url.path.startswith("/api/manual/"):
        return await request_validation_exception_handler(request, exc)
    messages = []
    for error in exc.errors()[:8]:
        location = " → ".join(f"第 {part + 1} 場" if isinstance(part, int) else FIELD_NAMES.get(part, "欄位")
                              for part in error["loc"] if part != "body")
        kind, context = error.get("type", ""), error.get("ctx", {})
        if kind == "value_error":
            message = str(error.get("msg", "")).removeprefix("Value error, ")
        elif kind == "missing":
            message = "此欄位必須填寫。"
        elif kind in {"datetime_parsing", "datetime_from_date_parsing", "datetime_type", "timezone_aware"}:
            message = "請輸入有效日期、時間及時區，例如 2026-10-25T20:00:00+08:00。"
        elif kind in {"int_type", "int_parsing"}:
            message = "入球必須為整數，不可使用小數或文字。"
        elif kind == "extra_forbidden":
            message = "包含不支援的欄位，請檢查輸入格式。"
        elif kind == "literal_error":
            message = "請選擇支援的分析模式或聯賽。"
        elif kind in {"less_than_equal", "greater_than_equal"}:
            message = f"數值須{'不大於 ' + str(context.get('le')) if 'le' in context else '不少於 ' + str(context.get('ge'))}。"
        elif kind in {"too_long", "string_too_long"}:
            message = f"內容過長，最多 {context.get('max_length', 1000)} 項或字元。"
        elif kind in {"finite_number", "float_type", "float_parsing"}:
            message = "請輸入有限數值，不可使用 NaN 或無限大。"
        else:
            message = "格式不正確，請檢查此欄位。"
        messages.append(f"{location}：{message}" if location else message)
    from fastapi.responses import JSONResponse
    return JSONResponse({"detail": "；".join(messages)}, status_code=422)


def custom_history(body: ManualAnalysisRequest, cutoff: str) -> tuple[list[dict], int]:
    records, identities, appearances = [], set(), set()
    assumed = 0
    instant = parsed(cutoff)
    for index, row in enumerate(body.historical_matches, 1):
        kickoff = row.kickoff.astimezone(timezone.utc)
        available = row.available_at or kickoff + timedelta(hours=36)
        available = available.astimezone(timezone.utc)
        if kickoff >= instant or available >= instant:
            raise HTTPException(422, f"第 {index} 場賽果不在分析截點之前可用。請移除未完賽、未公布或開賽後才知道的資料。")
        identity = (time_text(kickoff), row.home.casefold(), row.away.casefold())
        if identity in identities:
            raise HTTPException(422, f"第 {index} 場與其他賽果重複；重複資料不能增加樣本數。")
        team_times = {(time_text(kickoff), row.home.casefold()), (time_text(kickoff), row.away.casefold())}
        if appearances & team_times:
            raise HTTPException(422, f"第 {index} 場有球隊在同一時間出現多場比賽，請核對。")
        identities.add(identity)
        appearances.update(team_times)
        assumed += row.available_at is None
        digest = hashlib.sha256("|".join(identity).encode()).hexdigest()[:24]
        season_start = kickoff.year if kickoff.month >= 7 else kickoff.year - 1
        records.append({"id": "manual-result:" + digest, "league": body.league,
                        "season": f"{season_start}-{str(season_start + 1)[2:]}",
                        "home": row.home, "away": row.away, "kickoff": time_text(kickoff),
                        "kickoff_precision": "exact", "status": "FINISHED", "home_goals": row.home_goals,
                        "away_goals": row.away_goals, "result_available_at": time_text(available),
                        "source": "使用者手動賽果（未核實）", "source_url": None, "fetched_at": now()})
    return sorted(records, key=lambda value: (value["kickoff"], value["id"])), assumed


def empty_comparison(body: ManualAnalysisRequest, cutoff: str) -> dict:
    return {side: {"name": getattr(body, side), "league": body.league, "matches": 0,
                   "elo": None, "attack_strength": None, "defence_strength": None,
                   "form5": [], "form10": [], "goals_for_per_game": None, "goals_against_per_game": None,
                   "home": {"matches": 0}, "away": {"matches": 0}, "rest_days_to_match": None,
                   "xg": None, "xga": None, "injuries": None, "lineup": None,
                   "as_of": cutoff, "source": ["使用者自訂入球假設"], "source_urls": [],
                   "note": "此情境沒有使用球隊歷史資料；不能推算 Elo、狀態或傷停。"}
            for side in ("home", "away")}


def scenario_prediction(body: ManualAnalysisRequest, match: dict, cutoff: str, created: str) -> dict:
    from scipy.stats import poisson
    import numpy as np
    from .models import markets_from_matrix, normalize_matrix, probabilities_dict

    # Do not use the fitted model helper: it deliberately clips model rates to .08–7.
    # User scenarios must respect the actual entered rates, including zero and ten.
    rates = (float(body.home_expected_goals), float(body.away_expected_goals))
    side = max(3, int(poisson.ppf(1 - 1e-13, max(rates))) + 1)
    matrix = normalize_matrix(np.outer(poisson.pmf(np.arange(side), rates[0]), poisson.pmf(np.arange(side), rates[1])))
    h, a = np.indices(matrix.shape)
    return {"id": match["id"], "match_id": match["id"], "created_at": created, "as_of": cutoff,
            "model_version": "manual-independent-poisson-scenario-1", "match_snapshot": match,
            "expected_goals": {"home": float((matrix * h).sum()), "away": float((matrix * a).sum())},
            "probabilities": probabilities_dict(matrix), "markets": markets_from_matrix(matrix), "matrix": matrix.tolist(),
            "models": {}, "diagnostics": {"entered_rates": {"home": rates[0], "away": rates[1]},
                                          "calibrated": False, "trained": False, "notes_used_in_model": False},
            "confidence": {"label": "未經校準", "score": 0,
                           "reasons": ["使用者入球假設未經獨立驗證；0 代表沒有可驗證的信心評分，並非勝出概率。", "比分假設採用獨立 Poisson，沒有從球隊歷史估計攻守能力。"]},
            "data_quality": {"score": 0, "available": ["使用者輸入的賽事及入球假設（未核實）"],
                             "missing": ["經核實的歷史賽果", "樣本外校準", "真實 xG／xGA", "已確認傷停", "已確認正選陣容"],
                             "history_count": 0, "home_samples": 0, "away_samples": 0, "as_of": cutoff,
                             "source": "使用者自訂入球假設", "score_note": "沒有可評分的已核實數據，並非預測準確度。"},
            "source": "使用者自訂入球假設（未核實）", "historical": False, "manual": True,
            "expected_goals_label": "使用者假設入球（並非實測 xG 或已訓練模型估計）"}


def manual_report(body: ManualAnalysisRequest, prediction: dict, comparison: dict, warnings: list[str]) -> list[dict]:
    scenario = body.mode == "scenario"
    goals, probabilities = prediction["expected_goals"], prediction["probabilities"]
    favorite = max(probabilities, key=probabilities.get)
    label = {"home": "主隊勝出", "draw": "和局", "away": "客隊勝出"}[favorite]
    if scenario:
        strength = "此分析只採用你輸入的兩隊入球假設，沒有球隊實力、Elo 或近期狀態資料。"
        venue = "主客隊身份只用於區分兩個入球參數；沒有額外加上主場優勢。"
        form = "未輸入或使用歷史賽果，不能分析近期表現、對手質素、休息日或賽程密度。"
    else:
        home, away = comparison["home"], comparison["away"]
        strength = f"採用分析截點前可用的賽果：主隊 {home['matches']} 場、客隊 {away['matches']} 場；Elo 分別為 {home['elo']:.0f} 及 {away['elo']:.0f}，屬賽果推算評分。"
        venue = "模型從所選歷史資料估計主場效應及球隊攻守強度；手動賽事的場地與開賽時間未經外部核實。"
        form = f"最近最多五場的積分為主隊 {home.get('points_last5', 0)}、客隊 {away.get('points_last5', 0)}；Elo 更新考慮對手評分。只限已輸入或已收錄的賽果，不代表資料完整。"
    return [
        {"title": "兩隊實力比較", "text": strength},
        {"title": "主客場因素", "text": venue},
        {"title": "進攻與防守對位", "text": f"{'情境假設' if scenario else '模型估計'}主隊 {goals['home']:.2f} 球、客隊 {goals['away']:.2f} 球。這些並非實測 xG。"},
        {"title": "近期狀態及對手質素", "text": form},
        {"title": "傷停及正選陣容", "text": "沒有經核實的傷停、正選或真實 xG。補充文字只會保存供你參考，不會被當成已確認消息，亦不會改動數字概率。"},
        {"title": "預計入球情況", "text": f"同一比分分布的預期總入球為 {sum(goals.values()):.2f} 球；大小球、雙方入球及讓球均由該分布計算，不能據此確定比賽節奏。"},
        {"title": "最可能結果", "text": f"{'此假設下' if scenario else '此歷史模型下'}概率最高為{label}（{probabilities[favorite] * 100:.1f}%），並非保證。"},
        {"title": "不確定因素", "text": "；".join(warnings + prediction["data_quality"]["missing"])},
    ]


def analyze(body: ManualAnalysisRequest, service: FootballService) -> dict:
    from .models import ModelEngine

    created, analysis_id = now(), uuid.uuid4().hex
    cutoff = min(parsed(created), body.kickoff.astimezone(timezone.utc)).isoformat()
    match = {"id": "manual:" + analysis_id, "league": body.league, "season": "手動賽事",
             "home": body.home, "away": body.away, "kickoff": time_text(body.kickoff), "kickoff_precision": "exact",
             "status": "SCHEDULED", "home_goals": None, "away_goals": None,
             "source": "使用者手動輸入（未核實）", "source_url": None, "fetched_at": created}
    warnings = ["賽事及手動資料由使用者提供，未經外部核實；不會納入正式歷史、訓練、回測或準確度統計。",
                "補充文字及來源說明只作紀錄，沒有用於數字模型。"]
    retrospective = body.kickoff <= parsed(created)
    if retrospective:
        warnings.append("這是事後建立的假設分析，只按當時截點前記錄重建；不是當年實際發布的賽前預測。")
    history, assumed = [], 0
    if body.mode == "scenario":
        prediction = scenario_prediction(body, match, cutoff, created)
        comparison = empty_comparison(body, cutoff)
        warnings.append("這是未經訓練或校準的自訂入球情境，不能視為經驗證的機器學習預測。")
        data_source, availability_note = "使用者自訂入球假設", "沒有使用歷史賽果。"
        validated = False
    else:
        if body.mode == "history":
            from .service import known_history
            history = known_history(service.db.matches(body.league), cutoff)
            minimum = 80
            data_source = "已收錄的數據供應商歷史賽果"
            availability_note = "採用已記錄的賽果可用時間；回顧下載資料不能證明當時的實際發布時間。"
        else:
            history, assumed = custom_history(body, cutoff)
            minimum = 30
            data_source = "使用者手動歷史賽果（未核實）"
            availability_note = f"{assumed} 場未填賽果可用時間，保守假設在開賽 36 小時後可用；其他可用時間由使用者聲明，未經核實。"
            warnings.extend(["手動歷史賽果沒有獨立校準或樣本外驗證；預設使用 Dixon–Coles，Elo 只作比較。", availability_note])
        counts = {side: sum(getattr(body, side) in (row["home"], row["away"]) for row in history) for side in ("home", "away")}
        if len(history) < minimum or min(counts.values()) < 5:
            raise HTTPException(422, f"數據不足：此模式至少需要 {minimum} 場可用賽果及每隊 5 場；目前合共 {len(history)} 場、主隊 {counts['home']} 場、客隊 {counts['away']} 場。球隊名稱須與賽果完全一致。你亦可改用明確標示的自訂入球情境。")
        if body.mode == "history":
            engine, history = service.engine(body.league, cutoff)
        else:
            engine = ModelEngine(history, cutoff, config={"fit_gradient_boosting": False})
        raw = engine.predict(body.home, body.away, body.league, kickoff=match["kickoff"])
        if not raw.get("diagnostics", {}).get("fit_success", False):
            warnings.append("入球模型的數值擬合未完全收斂，結果需要審慎看待；已保留擬合狀態供檢查。")
        prediction = service.make_prediction(match, cutoff, raw, history)
        validated = bool(raw.get("diagnostics", {}).get("validation_configured"))
        if body.mode == "custom_history":
            # make_prediction considers the official league's configuration. That calibration
            # does not apply to unverified custom samples; remove every such implication.
            prediction["model_version"] = "manual-custom-" + raw["model_version"]
            prediction["confidence"] = {"label": "未經驗證", "score": 0,
                                        "reasons": ["手動賽果未經核實，沒有獨立校準或樣本外驗證。", "0 代表沒有可驗證的信心評分，並非勝出概率。"]}
            prediction["data_quality"]["score"] = 0
            prediction["data_quality"]["score_note"] = "只有未核實的使用者資料，不能評定已核實數據完整程度。"
            prediction["data_quality"]["available"] = ["使用者輸入的歷史比分（未核實）", "從輸入比分推算的 Elo、狀態及攻守能力"]
            prediction["data_quality"]["missing"].extend(["手動賽果的外部核實", "手動資料的獨立樣本外驗證及校準"])
            prediction["diagnostics"]["validation_configured"] = False
            prediction["diagnostics"]["calibrated"] = False
        prediction["confidence"]["reasons"].append("手動賽事的場地、對賽及開賽時間未經核實。")
        prediction.update(id="manual-prediction:" + analysis_id, created_at=created, manual=True,
                          historical=False, expected_goals_label="模型估計入球（並非實測 xG；手動分析）")
        prediction["diagnostics"]["notes_used_in_model"] = False
        comparison = {}
        for side in ("home", "away"):
            summary = service.team_summary(engine, getattr(body, side), body.league, cutoff)
            summary.update(xg=None, xga=None, injuries=None, lineup=None,
                           source_urls=sorted({row["source_url"] for row in history if row.get("source_url") and getattr(body, side) in (row["home"], row["away"])}),
                           note="統計只來自本次選用的歷史賽果；沒有實測 xG、已確認傷停或正選。",
                           rest_days_to_match=(body.kickoff - parsed(summary["last_match_at"])).total_seconds() / 86400 if summary.get("last_match_at") else None,
                           rest_days_note="只按已輸入或已收錄的上次賽果計算；缺少資料會高估休息日。")
            comparison[side] = summary
        if prediction["data_quality"].get("result_age_days", 0) > 7:
            warnings.append(f"所選資料的最新賽果已相隔 {prediction['data_quality']['result_age_days']} 日，近期覆蓋不足。")
        if not validated:
            warnings.append("沒有適用於這份資料的獨立校準結果，不能聲稱預測概率已驗證可靠。")
    payload = body.model_dump(mode="json")
    provenance = {"fixture": "使用者手動輸入，未經核實", "data_source": data_source, "source_note": body.source_note,
                  "as_of": cutoff, "history_count": len(history),
                  "home_samples": sum(body.home in (row["home"], row["away"]) for row in history),
                  "away_samples": sum(body.away in (row["home"], row["away"]) for row in history),
                  "availability_note": availability_note, "assumed_availability_count": assumed,
                  "validated": validated, "retrospective": retrospective,
                  "notes_used_in_model": False, "official_performance_included": False}
    result = {"id": analysis_id, "created_at": created, "mode": body.mode, "input": payload, "match": match,
              "prediction": prediction, "comparison": comparison, "warnings": warnings, "provenance": provenance,
              "data_gaps": prediction["data_quality"]["missing"], "report": manual_report(body, prediction, comparison, warnings)}
    service.db.save_manual_analysis(result)
    return result


def install_manual_routes(application: FastAPI, service_dependency, require_admin) -> None:
    application.add_exception_handler(RequestValidationError, manual_validation_error)

    @application.get("/api/manual/options", dependencies=[Depends(require_admin)])
    def options(league: League = "PL", s: FootballService = Depends(service_dependency)):
        return {"league": league, "teams": sorted({row[side] for row in s.db.matches(league) for side in ("home", "away")})}

    @application.post("/api/manual/analyses", dependencies=[Depends(require_admin)], status_code=201)
    def create(body: ManualAnalysisRequest, s: FootballService = Depends(service_dependency)):
        try:
            return analyze(body, s)
        except HTTPException:
            raise
        except Exception:
            logging.getLogger(__name__).exception("Manual analysis could not be completed")
            raise HTTPException(503, "手動分析暫時未能完成。請核對輸入資料後重試；如問題持續，請查看程式記錄。") from None

    @application.get("/api/manual/analyses", dependencies=[Depends(require_admin)])
    def listing(limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0),
                s: FootballService = Depends(service_dependency)):
        return s.db.manual_analyses(limit, offset)

    @application.get("/api/manual/analyses/{analysis_id}", dependencies=[Depends(require_admin)])
    def detail(analysis_id: str, s: FootballService = Depends(service_dependency)):
        value = s.db.manual_analysis(analysis_id)
        if value is None:
            raise HTTPException(404, "找不到手動分析紀錄。")
        return value
