"""Chronological, auditable research backtests with untouched final holdout.

Model refits use only results available at the next prediction origin. Calibration,
feature selection and ensemble weights are learned before the test period begins.
"""
from __future__ import annotations

import json
import math
from datetime import timedelta
from pathlib import Path

import numpy as np
from scipy.optimize import minimize, minimize_scalar

from .models import (MODEL_LABELS, MODEL_VERSION, ModelEngine, available_at,
                     calibrate_matrix, finished_records, iso, mix_matrices,
                     outcome_probabilities, probabilities_dict, utc)


def outcome(record: dict) -> int:
    return 0 if record["home_goals"] > record["away_goals"] else 1 if record["home_goals"] == record["away_goals"] else 2


def probability_metrics(probabilities: list | np.ndarray, labels: list | np.ndarray) -> dict:
    p, y = np.asarray(probabilities, dtype=float), np.asarray(labels, dtype=int)
    if len(y) == 0:
        return {"n": 0, "log_loss": None, "brier": None, "ece": None, "accuracy": None, "calibration_bins": []}
    if p.shape != (len(y), 3) or np.any(~np.isfinite(p)) or np.any(p < 0) or not np.allclose(p.sum(axis=1), 1):
        raise ValueError("Each prediction must contain three normalized probabilities")
    target = np.eye(3)[y]
    # Multiclass Brier score sums all three class errors (range 0..2).
    loss = -np.log(np.maximum(p[np.arange(len(y)), y], 1e-15)).mean()
    bins, ece = [], 0.0
    # Classwise calibration, averaged equally over home/draw/away. Reporting just
    # top-label confidence would hide badly calibrated draw probabilities.
    for cls, name in enumerate(("home", "draw", "away")):
        for i in range(10):
            mask = (p[:, cls] >= i/10) & ((p[:, cls] < (i+1)/10) if i < 9 else (p[:, cls] <= 1))
            n = int(mask.sum())
            if n:
                predicted, actual = float(p[mask, cls].mean()), float((y[mask] == cls).mean())
                ece += n / len(y) * abs(predicted-actual) / 3
                bins.append({"outcome": name, "lower": i/10, "upper": (i+1)/10, "n": n, "predicted": predicted, "observed": actual})
    return {"n": int(len(y)), "log_loss": float(loss), "brier": float(((p-target)**2).sum(axis=1).mean()), "ece": float(ece), "accuracy": float((p.argmax(axis=1) == y).mean()), "calibration_bins": bins}


def _calibrate_probabilities(p: np.ndarray, temperature: float) -> np.ndarray:
    logits = np.log(np.maximum(p, 1e-12))/temperature
    logits -= logits.max(axis=1, keepdims=True)
    result = np.exp(logits)
    return result/result.sum(axis=1, keepdims=True)


def fit_temperature(probabilities: np.ndarray, labels: np.ndarray) -> float:
    def objective(t):
        p = _calibrate_probabilities(probabilities, t)
        return -np.log(np.maximum(p[np.arange(len(labels)), labels], 1e-15)).mean()
    fitted = minimize_scalar(objective, bounds=(.65, 2.5), method="bounded")
    # Keep the uncalibrated model if temperature does not improve the dedicated
    # calibration set. No validation or test outcomes are consulted here.
    return float(fitted.x) if fitted.success and objective(fitted.x) < objective(1) else 1.0


def chronological_split(records: list[dict]) -> dict[str, list[dict]]:
    records = sorted(records, key=lambda r: (utc(r["kickoff"]), r["id"]))
    # Boundaries are unique UTC dates to keep same-day matches in the same stage.
    dates = sorted({utc(r["kickoff"]).date() for r in records})
    if len(dates) < 40:
        return {"train": records, "calibration": [], "validation": [], "test": []}
    cuts = [dates[min(len(dates)-1, max(1, int(len(dates)*fraction)))] for fraction in (.55, .70, .85)]
    groups = {name: [] for name in ("train", "calibration", "validation", "test")}
    for record in records:
        date = utc(record["kickoff"]).date()
        name = "train" if date < cuts[0] else "calibration" if date < cuts[1] else "validation" if date < cuts[2] else "test"
        groups[name].append(record)
    return groups


def _period(records: list[dict]) -> dict:
    return {"start": records[0]["kickoff"] if records else None, "end": records[-1]["kickoff"] if records else None, "n": len(records), "seasons": sorted({r.get("season", "unknown") for r in records})}


def _walk_forward(records: list[dict], evaluated: list[dict], league: str) -> list[dict]:
    engine, fit_at, rows = None, None, []
    for record in evaluated:
        cutoff = utc(record["kickoff"])
        # Date-only fixtures use midnight as conservative feature cutoff, not a
        # verified kickoff. Results from this date cannot become input features.
        if engine is None or (cutoff-fit_at).days >= 45:
            engine, fit_at = ModelEngine(records, iso(cutoff)), cutoff
        else:
            engine.advance(iso(cutoff))
        predicted = engine.predict(record["home"], record["away"], league)
        rows.append({"match": record, "raw_matrices": {name: item["matrix"] for name, item in predicted["models"].items()}, "fit_as_of": iso(fit_at), "known_results": predicted["diagnostics"]["known_results"]})
    return rows


def _matrix_rows(rows: list[dict], name: str, temperature: float) -> list[np.ndarray]:
    return [calibrate_matrix(row["raw_matrices"][name], temperature) for row in rows]


def _market_metrics(matrices: list[np.ndarray], records: list[dict]) -> dict:
    metrics = {}
    for name in ("over_2_5", "btts"):
        predicted, observed = [], []
        for matrix, record in zip(matrices, records):
            h, a = np.indices(matrix.shape)
            probability = float(matrix[h+a > 2.5].sum()) if name == "over_2_5" else float(matrix[(h > 0) & (a > 0)].sum())
            predicted.append(probability)
            observed.append(int(record["home_goals"]+record["away_goals"] > 2.5) if name == "over_2_5" else int(record["home_goals"] > 0 and record["away_goals"] > 0))
        p, y = np.clip(np.asarray(predicted), 1e-15, 1-1e-15), np.asarray(observed)
        metrics[name] = {"n": len(y), "log_loss": float(-(y*np.log(p)+(1-y)*np.log1p(-p)).mean()), "brier": float(((p-y)**2).mean())}
    # Correct-score likelihood checks the matrix beyond 1X2 calibration.
    scores = []
    for matrix, record in zip(matrices, records):
        h, a = record["home_goals"], record["away_goals"]
        scores.append(matrix[h, a] if h < matrix.shape[0] and a < matrix.shape[1] else 1e-15)
    metrics["correct_score"] = {"n": len(scores), "log_loss": float(-np.log(np.maximum(scores, 1e-15)).mean())}
    return metrics


def run_backtest(matches: list[dict], league: str, artifact_path: str | Path | None = None) -> dict:
    records = [r for r in finished_records(matches) if r.get("league") == league]
    groups = chronological_split(records)
    embargoed = []
    # Outcome availability must precede the NEXT stage, including when 36-hour
    # date-only availability straddles a boundary. The final test is untouched.
    for stage, next_stage in (("calibration", "validation"), ("validation", "test")):
        if groups[next_stage]:
            boundary = utc(groups[next_stage][0]["kickoff"])
            embargoed.extend(r["id"] for r in groups[stage] if available_at(r) >= boundary)
            groups[stage] = [r for r in groups[stage] if available_at(r) < boundary]
    report = {
        "league": league, "model_version": MODEL_VERSION, "status": "insufficient_data", "historical": True,
        "sample_count": 0, "planned_test_count": len(groups["test"]), "total_matches": len(records), "seasons": sorted({r.get("season", "unknown") for r in records}),
        **{f"{stage}_period": _period(rows) for stage, rows in groups.items()},
        "models": [], "prediction_rows": [], "ensemble_weights": {}, "inference_config": {}, "boundary_embargo": {"n": len(embargoed), "match_ids": embargoed, "reason": "Labels unavailable before next calibration/validation/test boundary excluded from selection"},
        "methodology": {"split": "Chronological unique-date 55% initial train / 15% calibration / 15% validation / 15% untouched test", "walk_forward": "Refit every 45 days; rolling/Elo inputs update only after declared result availability", "metrics": "1X2 log loss; unscaled multiclass Brier (0..2); classwise ECE; accuracy; binary goal markets and score likelihood", "selection": "Temperature fitted on calibration only; contextual feature group and ensemble weights selected on validation only; all models evaluated on all test matches"},
        "limitations": ["歷史資料屬事後研究回測，不能證明當日資料實際發布時間；使用保守的結果可用時間。", "日期精度資料使用該日 00:00 UTC 作保守截點，並非確認開賽時間。", "沒有真實 xG、傷停或陣容特徵；不代表提供商的預測。", "1X2 溫度校準會一致地重加權比分矩陣；入球市場未獨立校準，須另看樣本外市場指標。", "單一時間保留期及少量球季不能證明穩定優勢；測試期間所有比賽均有保留。"],
    }
    if len(groups["train"]) < 180 or any(len(groups[stage]) < 35 for stage in ("calibration", "validation", "test")):
        report["limitations"].append("數據不足：至少需要 180 場初始訓練及校準、驗證、測試各 35 場；尚未宣稱模型可靠。")
        return report
    # Available-at may lag first evaluated kickoff. The engine itself filters it;
    # the nominal initial training range is never treated as proof of availability.
    cal_rows = _walk_forward(records, groups["calibration"], league)
    validation_rows = _walk_forward(records, groups["validation"], league)
    names = sorted(set.intersection(*(set(row["raw_matrices"]) for row in cal_rows+validation_rows)))
    cal_y = np.array([outcome(r) for r in groups["calibration"]])
    validation_y = np.array([outcome(r) for r in groups["validation"]])
    temperatures, validation_p, validation_metrics, calibration_metrics = {}, {}, {}, {}
    for name in names:
        raw_p = np.array([outcome_probabilities(row["raw_matrices"][name]) for row in cal_rows])
        temperatures[name] = fit_temperature(raw_p, cal_y)
        calibration_metrics[name] = {"before": probability_metrics(raw_p, cal_y), "after": probability_metrics(_calibrate_probabilities(raw_p, temperatures[name]), cal_y), "temperature": temperatures[name]}
        validation_p[name] = np.array([outcome_probabilities(m) for m in _matrix_rows(validation_rows, name, temperatures[name])])
        validation_metrics[name] = probability_metrics(validation_p[name], validation_y)

    # Only retain the extra rest/congestion/venue/opponent/season feature group if
    # it improves independent validation loss against compact rolling/Elo inputs.
    gb_name = "gradient_boosting"
    if "gradient_boosting_compact" in names:
        compact_loss = validation_metrics["gradient_boosting_compact"]["log_loss"]
        full_loss = validation_metrics.get("gradient_boosting", {}).get("log_loss", math.inf)
        if full_loss >= compact_loss - .002:
            gb_name = "gradient_boosting_compact"
    allowed = [name for name in ("baseline", "dixon_coles", "elo", gb_name) if name in names]
    baseline_loss = validation_metrics["baseline"]["log_loss"]
    allowed = [name for name in allowed if name == "baseline" or validation_metrics[name]["log_loss"] <= baseline_loss + .01]
    stack = np.array([validation_p[name] for name in allowed])

    def ensemble_loss(w):
        p = np.einsum("m,mnc->nc", w, stack)
        return float(-np.log(np.maximum(p[np.arange(len(validation_y)), validation_y], 1e-15)).mean())

    best = min(allowed, key=lambda name: validation_metrics[name]["log_loss"])
    initial = np.array([float(name == best) for name in allowed])
    optimizer = minimize(ensemble_loss, initial, method="SLSQP", bounds=[(0, 1)]*len(allowed), constraints={"type": "eq", "fun": lambda w: w.sum()-1}, options={"maxiter": 180, "ftol": 1e-10})
    learned = optimizer.x if optimizer.success and ensemble_loss(optimizer.x) < ensemble_loss(initial)-.0001 else initial
    learned = np.maximum(learned, 0)
    learned /= learned.sum()
    weights = {name: float(weight) for name, weight in zip(allowed, learned) if weight > 1e-8}
    # Selection is complete before test models or labels are evaluated.
    validation_ensemble = np.einsum("m,mnc->nc", learned, stack)
    validation_metrics["ensemble"] = probability_metrics(validation_ensemble, validation_y)
    report.update({"status": "completed", "ensemble_weights": weights, "selected_model": "ensemble" if len(weights) > 1 else next(iter(weights)), "calibration": calibration_metrics, "validation_models": [{"name": name, "label": MODEL_LABELS[name], **metrics} for name, metrics in validation_metrics.items()], "feature_selection": {"selected": gb_name, "criterion": "Full contextual group retained only when validation log loss improves by at least 0.002 over compact form/Elo features", "real_xg_used": False, "injuries_used": False}, "inference_config": {"weights": weights, "temperatures": temperatures, "validated": True, "feature_profile": gb_name, "calibration_metrics": {"ece": validation_metrics["ensemble"]["ece"], "log_loss": validation_metrics["ensemble"]["log_loss"]}, "selected_before": groups["test"][0]["kickoff"], "trained_until": iso(max(available_at(r) for r in groups["validation"]))}})

    test_rows = _walk_forward(records, groups["test"], league)
    test_y = np.array([outcome(r) for r in groups["test"]])
    test_matrices = {name: _matrix_rows(test_rows, name, temperatures[name]) for name in names}
    test_matrices["ensemble"] = [mix_matrices({name: test_matrices[name][i] for name in names}, weights) for i in range(len(test_rows))]
    for name, matrices in test_matrices.items():
        p = np.array([outcome_probabilities(m) for m in matrices])
        report["models"].append({"name": name, "label": MODEL_LABELS[name], **probability_metrics(p, test_y), "market_metrics": _market_metrics(matrices, groups["test"])})
    for i, row in enumerate(test_rows):
        record, matrix = row["match"], test_matrices["ensemble"][i]
        report["prediction_rows"].append({"match_id": record["id"], "kickoff": record["kickoff"], "as_of": record["kickoff"], "fit_as_of": row["fit_as_of"], "home": record["home"], "away": record["away"], "league": league, "model_version": MODEL_VERSION, "known_results": row["known_results"], "matrix": matrix.tolist(), "probabilities": probabilities_dict(matrix), "model_probabilities": {name: probabilities_dict(matrices[i]) for name, matrices in test_matrices.items()}, "actual": {"home_goals": record["home_goals"], "away_goals": record["away_goals"], "outcome": ("home", "draw", "away")[test_y[i]]}, "source": "walk-forward-backtest", "historical": True})
    report["sample_count"] = len(report["prediction_rows"])
    if artifact_path:
        destination = Path(artifact_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(report, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    return report
