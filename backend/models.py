"""Own, trainable football models; every market is derived from one score matrix.

Historical records are admitted only after their declared result availability.
Retrospective availability is an assumption, never an as-published provenance claim.
"""
from __future__ import annotations

import math
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

import numpy as np
from scipy.optimize import minimize
from scipy.special import gammaln
from scipy.stats import poisson
from sklearn.ensemble import HistGradientBoostingRegressor
from threadpoolctl import threadpool_limits


def _thread_budget() -> int:
    try:
        requested = int(os.environ.get("MODEL_THREADS", os.environ.get("OMP_NUM_THREADS", "2")))
    except ValueError:
        requested = 2
    return max(1, min(requested, 4, os.cpu_count() or 1))


MODEL_THREADS = _thread_budget()

MODEL_VERSION = "fia-dc-elo-gb-1.0"
MODEL_NAMES = ("baseline", "dixon_coles", "elo", "gradient_boosting", "gradient_boosting_compact")
MODEL_LABELS = {"baseline": "聯賽歷史基準", "dixon_coles": "Dixon–Coles", "elo": "動態 Elo", "gradient_boosting": "Gradient Boosting（完整特徵）", "gradient_boosting_compact": "Gradient Boosting（精簡特徵）", "ensemble": "驗證期選定組合"}


def utc(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        return value.replace(tzinfo=value.tzinfo or timezone.utc).astimezone(timezone.utc)
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.replace(tzinfo=parsed.tzinfo or timezone.utc).astimezone(timezone.utc)


def iso(value: datetime) -> str:
    return utc(value).isoformat().replace("+00:00", "Z")


def available_at(match: dict) -> datetime:
    if match.get("result_available_at"):
        return utc(match["result_available_at"])
    return utc(match["kickoff"]) + timedelta(hours=36 if match.get("kickoff_precision", "date") == "date" else 3)


def finished_records(matches: list[dict]) -> list[dict]:
    records: dict[str, dict] = {}
    for match in matches:
        if match.get("status") != "FINISHED" or match.get("home_goals") is None or match.get("away_goals") is None:
            continue
        if not match.get("home") or not match.get("away") or match["home"] == match["away"]:
            continue
        try:
            h, a = int(match["home_goals"]), int(match["away_goals"])
            if h < 0 or a < 0 or h > 30 or a > 30:
                continue
            utc(match["kickoff"])
            available_at(match)
        except (TypeError, ValueError, KeyError):
            continue
        key = str(match.get("id") or f"{match.get('league')}:{match['kickoff']}:{match['home']}:{match['away']}")
        records[key] = {**match, "home_goals": h, "away_goals": a, "id": key}
    return sorted(records.values(), key=lambda r: (utc(r["kickoff"]), r["id"]))


def normalize_matrix(matrix: Any) -> np.ndarray:
    value = np.asarray(matrix, dtype=float)
    if value.ndim != 2 or min(value.shape) < 2 or not np.all(np.isfinite(value)) or np.any(value < 0) or value.sum() <= 0:
        raise ValueError("Score matrix must contain finite nonnegative probability mass")
    return value / value.sum()


def outcome_probabilities(matrix: Any) -> np.ndarray:
    value = normalize_matrix(matrix)
    h, a = np.indices(value.shape)
    return np.array([value[h > a].sum(), value[h == a].sum(), value[h < a].sum()])


def probabilities_dict(matrix: Any) -> dict[str, float]:
    p = outcome_probabilities(matrix)
    return {name: float(value) for name, value in zip(("home", "draw", "away"), p)}


def outcome_reweight(matrix: Any, target: Any) -> np.ndarray:
    value = normalize_matrix(matrix)
    target = np.maximum(np.asarray(target, dtype=float), 1e-12)
    target /= target.sum()
    current = outcome_probabilities(value)
    h, a = np.indices(value.shape)
    for mask, actual, desired in zip((h > a, h == a, h < a), current, target):
        if actual > 0:
            value[mask] *= desired / actual
    return normalize_matrix(value)


def calibrate_matrix(matrix: Any, temperature: float = 1.0) -> np.ndarray:
    p = np.maximum(outcome_probabilities(matrix), 1e-12)
    logits = np.log(p) / max(float(temperature), 0.1)
    logits -= logits.max()
    return outcome_reweight(matrix, np.exp(logits))


def poisson_matrix(home_rate: float, away_rate: float, rho: float = 0.0, size: int | None = None) -> np.ndarray:
    lh, la = np.clip([home_rate, away_rate], 0.08, 7.0)
    # Both axes use the same support, making ensemble mixing unambiguous.
    size = size or min(30, max(13, int(poisson.ppf(1 - 1e-11, max(lh, la))) + 1))
    matrix = np.outer(poisson.pmf(np.arange(size), lh), poisson.pmf(np.arange(size), la))
    # Keep the low-score adjustment positive at prediction-time for promoted teams.
    rho = float(np.clip(rho, max(-0.18, -0.95 / max(lh, la)), min(0.08, 0.95 / (lh * la))))
    matrix[0, 0] *= 1 - lh * la * rho
    matrix[0, 1] *= 1 + lh * rho
    matrix[1, 0] *= 1 + la * rho
    matrix[1, 1] *= 1 - rho
    return normalize_matrix(matrix)


def mix_matrices(matrices: dict[str, Any], weights: dict[str, float]) -> np.ndarray:
    usable = {name: float(weight) for name, weight in weights.items() if name in matrices and weight > 0}
    if not usable:
        raise ValueError("At least one available model must have positive ensemble weight")
    side = max(max(np.asarray(matrices[name]).shape) for name in usable)
    result = np.zeros((side, side))
    for name, weight in usable.items():
        value = normalize_matrix(matrices[name])
        result[:value.shape[0], :value.shape[1]] += value * weight
    return normalize_matrix(result)


def asian_handicap(matrix: Any, line: float) -> dict[str, float]:
    """Home-team settlement probabilities, including quarter-line split stakes."""
    if not math.isfinite(line) or abs(line * 4 - round(line * 4)) > 1e-8:
        raise ValueError("Asian handicap lines must be multiples of 0.25")
    value = normalize_matrix(matrix)
    h, a = np.indices(value.shape)
    margin = h - a
    quarter = int(round(line * 4)) % 2 != 0
    halves = (math.floor(line * 2) / 2, math.ceil(line * 2) / 2) if quarter else (line, line)
    first, second = (np.sign(margin + component).astype(int) for component in halves)
    state = first + second
    return {"line": float(line), **{name: float(value[state == score].sum()) for name, score in (("full_win", 2), ("half_win", 1), ("push", 0), ("half_loss", -1), ("full_loss", -2))}}


def markets_from_matrix(matrix: Any) -> dict:
    value = normalize_matrix(matrix)
    h, a = np.indices(value.shape)
    goals = h + a
    btts = float(value[(h > 0) & (a > 0)].sum())
    ordered = np.argsort(value.ravel())[::-1][:5]
    return {
        "over_under": [{"line": line, "over": float(value[goals > line].sum()), "under": float(value[goals < line].sum())} for line in (1.5, 2.5, 3.5)],
        "btts": {"yes": btts, "no": 1 - btts},
        "scores": [{"home": int(index // value.shape[1]), "away": int(index % value.shape[1]), "probability": float(value.ravel()[index])} for index in ordered],
        "total_goals": [{"goals": total, "probability": float(value[goals == total].sum())} for total in range(value.shape[0] + value.shape[1] - 1)],
        "asian_handicap": [asian_handicap(value, line) for line in np.arange(-2, 2.01, .25)],
    }


class _State:
    def __init__(self):
        self.elo = defaultdict(lambda: 1500.0)
        self.games: dict[str, list[dict]] = defaultdict(list)

    def add(self, match: dict):
        home, away = match["home"], match["away"]
        eh, ea = self.elo[home], self.elo[away]
        expected = 1 / (1 + 10 ** (-(eh + 65 - ea) / 400))
        hg, ag = match["home_goals"], match["away_goals"]
        result = 1.0 if hg > ag else .5 if hg == ag else 0.0
        change = 20 * (1 + .25 * math.log1p(abs(hg - ag))) * (result - expected)
        self.elo[home] += change
        self.elo[away] -= change
        for team, opponent, gf, ga, venue, opposite_elo in ((home, away, hg, ag, "home", ea), (away, home, ag, hg, "away", eh)):
            self.games[team].append({"id": match["id"], "kickoff": match["kickoff"], "opponent": opponent, "goals_for": gf, "goals_against": ga, "venue": venue, "points": 3 if gf > ga else 1 if gf == ga else 0, "result": "W" if gf > ga else "D" if gf == ga else "L", "own_elo_before": eh if venue == "home" else ea, "opponent_elo_before": opposite_elo, "elo_expected_before": expected if venue == "home" else 1-expected, "elo_result": 1.0 if gf > ga else .5 if gf == ga else 0.0, "source": match.get("source"), "source_url": match.get("source_url")})


def _team_features(state: _State, team: str, at: datetime, venue: str) -> list[float]:
    games = sorted(state.games[team], key=lambda r: utc(r["kickoff"]))
    # Priors supply numeric inputs; coverage is separately reported, never hidden.
    features = []
    for count in (5, 10):
        recent = games[-count:]
        n = len(recent)
        features.extend([(sum(g["goals_for"] for g in recent) + 2 * 1.3) / (n + 2), (sum(g["goals_against"] for g in recent) + 2 * 1.3) / (n + 2), (sum(g["points"] for g in recent) + 2 * 1.3) / (n + 2)])
    relevant = [g for g in games if g["venue"] == venue][-10:]
    n = len(relevant)
    features.extend([(sum(g["goals_for"] for g in relevant) + 2.6) / (n + 2), (sum(g["goals_against"] for g in relevant) + 2.6) / (n + 2)])
    rest = max(0.0, (at - utc(games[-1]["kickoff"])).total_seconds() / 86400) if games else 14.0
    recent = games[-10:]
    adjusted = sum((g["points"] / 3 - 1 / (1 + 10 ** ((g["opponent_elo_before"] - 1500) / 400))) for g in recent) / max(1, len(recent))
    features.extend([min(rest, 45), sum(0 <= (at - utc(g["kickoff"])).total_seconds() / 86400 < 14 for g in games), adjusted, min(len(games), 60) / 60])
    return features


def _features(state: _State, home: str, away: str, at: datetime) -> np.ndarray:
    return np.array(_team_features(state, home, at, "home") + _team_features(state, away, at, "away") + [(state.elo[home] - state.elo[away]) / 400, math.sin(2 * math.pi * at.month / 12), math.cos(2 * math.pi * at.month / 12)], dtype=float)


def training_features(records: list[dict]) -> tuple[np.ndarray, np.ndarray]:
    """Replay result-availability events, independently of labels being prepared."""
    records = finished_records(records)
    events = sorted(records, key=lambda r: (available_at(r), r["id"]))
    state, pointer, features, goals = _State(), 0, [], []
    for record in records:
        at = utc(record["kickoff"])
        while pointer < len(events) and available_at(events[pointer]) < at:
            state.add(events[pointer])
            pointer += 1
        features.append(_features(state, record["home"], record["away"], at))
        goals.append([record["home_goals"], record["away_goals"]])
    return np.asarray(features), np.asarray(goals)


class _LeagueModel:
    def __init__(self, records: list[dict], cutoff: datetime):
        self.records, self.cutoff = records, cutoff
        self.teams = sorted({r[key] for r in records for key in ("home", "away")})
        self.index = {name: i for i, name in enumerate(self.teams)}
        n = len(self.teams)
        self.attack, self.defence = np.zeros(n), np.zeros(n)
        self.intercept, self.home_advantage, self.rho = math.log(1.25), math.log(1.2), 0.0
        self.fit_success = False
        self.fit_message = "Fewer than 30 available league results; prior-based estimate"
        self.boosts: dict[str, tuple[Any, Any, Any]] = {}
        if records:
            self.home_mean = (sum(r["home_goals"] for r in records) + 3) / (len(records) + 2)
            self.away_mean = (sum(r["away_goals"] for r in records) + 2.5) / (len(records) + 2)
            counts = np.bincount([0 if r["home_goals"] > r["away_goals"] else 1 if r["home_goals"] == r["away_goals"] else 2 for r in records], minlength=3) + 1
            self.baseline = counts / counts.sum()
            self.intercept = math.log(max(.2, self.away_mean))
            self.home_advantage = math.log(self.home_mean / self.away_mean)
        else:
            self.home_mean, self.away_mean = 1.5, 1.25
            self.baseline = outcome_probabilities(poisson_matrix(1.5, 1.25))
        if len(records) >= 30:
            self._fit_dc()
        if len(records) >= 160:
            self._fit_boosts()

    def _fit_dc(self):
        n = len(self.teams)
        homes = np.array([self.index[r["home"]] for r in self.records])
        aways = np.array([self.index[r["away"]] for r in self.records])
        hg = np.array([r["home_goals"] for r in self.records], dtype=float)
        ag = np.array([r["away_goals"] for r in self.records], dtype=float)
        ages = np.array([(self.cutoff - utc(r["kickoff"])).total_seconds() / 86400 for r in self.records])
        weights = np.exp(-math.log(2) * ages / 365)
        ridge = 3.0
        masks = [(hg == h) & (ag == a) for h, a in ((0, 0), (0, 1), (1, 0), (1, 1))]

        def loss(parameters):
            intercept, home_adv, rho = parameters[:3]
            attack, defence = parameters[3:3+n], parameters[3+n:]
            attack_c, defence_c = attack - attack.mean(), defence - defence.mean()
            lh = np.exp(intercept + home_adv + attack_c[homes] + defence_c[aways])
            la = np.exp(intercept + attack_c[aways] + defence_c[homes])
            tau, dl, da, dr = np.ones(len(hg)), np.zeros(len(hg)), np.zeros(len(hg)), np.zeros(len(hg))
            m00, m01, m10, m11 = masks
            tau[m00] = 1 - lh[m00] * la[m00] * rho
            tau[m01] = 1 + lh[m01] * rho
            tau[m10] = 1 + la[m10] * rho
            tau[m11] = 1 - rho
            dl[m00], da[m00], dr[m00] = -lh[m00]*la[m00]*rho, -lh[m00]*la[m00]*rho, -lh[m00]*la[m00]
            dl[m01], dr[m01] = lh[m01]*rho, lh[m01]
            da[m10], dr[m10] = la[m10]*rho, la[m10]
            dr[m11] = -1
            if np.any(tau <= 0):
                # Reject invalid Dixon–Coles probabilities without dropping records.
                return 1e12 + float(np.maximum(-tau, 0).sum()) * 1e9, np.zeros_like(parameters)
            objective = np.sum(weights * (lh - hg*np.log(lh) + gammaln(hg+1) + la - ag*np.log(la) + gammaln(ag+1) - np.log(tau))) + ridge*(attack@attack + defence@defence)
            dh = weights*(lh - hg - dl/tau)
            dav = weights*(la - ag - da/tau)
            gatk = np.bincount(homes, dh, minlength=n) + np.bincount(aways, dav, minlength=n)
            gdef = np.bincount(aways, dh, minlength=n) + np.bincount(homes, dav, minlength=n)
            gradient = np.r_[np.sum(dh+dav), np.sum(dh), -np.sum(weights*dr/tau), gatk-gatk.mean()+2*ridge*attack, gdef-gdef.mean()+2*ridge*defence]
            return float(objective), gradient

        initial = np.r_[self.intercept, self.home_advantage, -.04, np.zeros(n*2)]
        result = minimize(loss, initial, method="L-BFGS-B", jac=True, bounds=[(-2, 2), (-.5, .8), (-.10, .02)] + [(-1.5, 1.5)]*(2*n), options={"maxiter": 180, "ftol": 1e-9})
        if np.isfinite(result.fun) and result.fun < 1e11:
            self.intercept, self.home_advantage, self.rho = map(float, result.x[:3])
            self.attack, self.defence = result.x[3:3+n], result.x[3+n:]
            self.attack -= self.attack.mean()
            self.defence -= self.defence.mean()
        self.fit_success, self.fit_message = bool(result.success), str(result.message)

    @threadpool_limits.wrap(limits=MODEL_THREADS)
    def _fit_boosts(self):
        x, y = training_features(self.records)
        weights = np.array([math.exp(-math.log(2)*(self.cutoff-utc(r["kickoff"])).total_seconds()/86400/540) for r in self.records])
        # A chronological outer validation chooses contextual vs compact inputs.
        compact = np.array([0, 1, 2, 3, 4, 5, 12, 13, 14, 15, 16, 17, 24])
        for name, columns in (("gradient_boosting", np.arange(x.shape[1])), ("gradient_boosting_compact", compact)):
            fitted = []
            for target in (0, 1):
                estimator = HistGradientBoostingRegressor(loss="poisson", learning_rate=.06, max_iter=75, max_leaf_nodes=9, min_samples_leaf=30, l2_regularization=2, early_stopping=False, random_state=37)
                estimator.fit(x[:, columns], y[:, target], sample_weight=weights)
                fitted.append(estimator)
            self.boosts[name] = (fitted[0], fitted[1], columns)

    def rates(self, home: str, away: str) -> tuple[float, float]:
        hi, ai = self.index.get(home), self.index.get(away)
        ha = self.attack[hi] if hi is not None else 0
        hd = self.defence[hi] if hi is not None else 0
        aa = self.attack[ai] if ai is not None else 0
        ad = self.defence[ai] if ai is not None else 0
        return float(np.clip(math.exp(self.intercept+self.home_advantage+ha+ad), .08, 7)), float(np.clip(math.exp(self.intercept+aa+hd), .08, 7))


class ModelEngine:
    def __init__(self, history: list[dict], as_of: str, config: dict | None = None):
        self.as_of = utc(as_of)
        self.config = config or {}
        self.history = finished_records(history)
        self.known = [r for r in self.history if available_at(r) < self.as_of and utc(r["kickoff"]) < self.as_of]
        self.leagues: dict[str, _LeagueModel] = {}
        self.states: dict[str, _State] = {}
        self._refresh_states()

    def _refresh_states(self):
        self.states = {}
        for record in sorted(self.known, key=lambda r: (available_at(r), r["id"])):
            self.states.setdefault(record["league"], _State()).add(record)

    def advance(self, as_of: str, history: list[dict] | None = None):
        """Advance rolling/Elo inputs; parameters remain frozen until the next origin."""
        at = utc(as_of)
        if at < self.as_of:
            raise ValueError("A fitted engine cannot be moved backwards in time")
        if history is not None:
            self.history = finished_records(history)
        new = [r for r in self.history if available_at(r) < at and utc(r["kickoff"]) < at]
        previous_ids = {r["id"] for r in self.known}
        for record in sorted((r for r in new if r["id"] not in previous_ids), key=lambda r: (available_at(r), r["id"])):
            self.states.setdefault(record["league"], _State()).add(record)
        self.known, self.as_of = new, at

    def _league(self, league: str) -> _LeagueModel:
        if league not in self.leagues:
            self.leagues[league] = _LeagueModel([r for r in self.known if r["league"] == league], self.as_of)
        return self.leagues[league]

    @threadpool_limits.wrap(limits=MODEL_THREADS)
    def predict(self, home: str, away: str, league: str, kickoff: str | None = None) -> dict:
        if home == away:
            raise ValueError("A team cannot play itself")
        model, state = self._league(league), self.states.setdefault(league, _State())
        lh, la = model.rates(home, away)
        matrices = {"dixon_coles": poisson_matrix(lh, la, model.rho)}
        baseline = poisson_matrix(model.home_mean, model.away_mean)
        matrices["baseline"] = outcome_reweight(baseline, model.baseline)
        elo_diff = state.elo[home] + 65 - state.elo[away]
        expected = 1/(1+10**(-elo_diff/400))
        draw = float(np.clip(model.baseline[1]*math.exp(-abs(elo_diff)/850), .08, .40))
        elo_p = [(1-draw)*expected, draw, (1-draw)*(1-expected)]
        matrices["elo"] = outcome_reweight(baseline, elo_p)
        feature_at = max(self.as_of, utc(kickoff)) if kickoff else self.as_of
        feature = _features(state, home, away, feature_at)
        for name, (home_fit, away_fit, columns) in model.boosts.items():
            rates = [float(home_fit.predict(feature[columns].reshape(1, -1))[0]), float(away_fit.predict(feature[columns].reshape(1, -1))[0])]
            matrices[name] = poisson_matrix(*rates)
        temperatures = self.config.get("temperatures", {})
        calibrated = {name: calibrate_matrix(matrix, temperatures.get(name, 1.0)) for name, matrix in matrices.items()}
        weights = self.config.get("weights") or {"dixon_coles": 1.0}
        matrix = mix_matrices(calibrated, weights)
        h, a = np.indices(matrix.shape)
        games_home, games_away = len(state.games[home]), len(state.games[away])
        model_p = {name: probabilities_dict(value) for name, value in calibrated.items()}
        probabilities = probabilities_dict(matrix)
        std = float(np.std(np.array([list(p.values()) for p in model_p.values()]), axis=0).mean())
        coverage = min(1., min(games_home, games_away)/15) * min(1., len(model.records)/400)
        calibrated_score = max(.2, 1-float(self.config.get("calibration_metrics", {}).get("ece", .18))*2)
        confidence = float(np.clip(100*coverage*calibrated_score*(1-min(.7, std*3)), 0, 100))
        return {
            "matrix": matrix.tolist(), "expected_goals": {"home": float((matrix*h).sum()), "away": float((matrix*a).sum())},
            "probabilities": probabilities, "markets": markets_from_matrix(matrix),
            "models": {name: {"probabilities": model_p[name], "matrix": value.tolist()} for name, value in calibrated.items()},
            "model_version": MODEL_VERSION, "as_of": iso(self.as_of),
            "diagnostics": {"training_matches": len(model.records), "known_results": len([r for r in self.known if r["league"] == league]), "home_matches": games_home, "away_matches": games_away, "fit_success": model.fit_success, "fit_message": model.fit_message, "rho": model.rho, "ensemble_weights": weights, "temperatures": temperatures, "model_disagreement": std, "validation_configured": bool(self.config.get("validated")), "features": "Prior-only scores/form/venue/Elo/rest/congestion/opponent context; no xG or injuries"},
            "confidence": {"score": round(confidence, 1), "label": "中等" if confidence >= 55 else "有限" if confidence >= 30 else "數據不足", "reasons": [f"兩隊可用歷史賽事：{games_home}／{games_away}", "信心綜合歷史覆蓋、校準及模型分歧，並非最高勝出概率", "未有獨立驗證設定" if not self.config.get("validated") else "使用歷史驗證期選定設定"]},
            "data_quality": {"score": round(coverage*100, 1), "available": ["來源記錄賽果", "主客場", "賽程日期", "結果推算的 Elo"], "missing": ["真實 xG／xGA", "已確認傷停", "正選陣容", "歷史資料即時發布時間"]},
        }

    def team_summary(self, team: str, league: str) -> dict:
        model, state = self._league(league), self.states.setdefault(league, _State())
        games = sorted(state.games[team], key=lambda g: utc(g["kickoff"]), reverse=True)
        index = model.index.get(team)
        venue_stats = {}
        for venue in ("home", "away"):
            relevant = [game for game in games if game["venue"] == venue]
            n = len(relevant)
            venue_stats[venue] = {"matches": n, "goals_for_per_game": sum(g["goals_for"] for g in relevant)/n if n else None, "goals_against_per_game": sum(g["goals_against"] for g in relevant)/n if n else None, "points_per_game": sum(g["points"] for g in relevant)/n if n else None}
        n = len(games)
        recent = games[:10]
        return {"name": team, "league": league, "matches": n, "elo": round(state.elo[team], 1), "attack_strength": float(math.exp(model.attack[index])) if index is not None else None, "defence_strength": float(math.exp(model.defence[index])) if index is not None else None, "strength_notes": {"attack": "相對聯賽平均的入球倍率，較高代表較強攻力", "defence": "相對聯賽平均的失球倍率，較低代表較強防守"}, "goals_for": sum(g["goals_for"] for g in games), "goals_against": sum(g["goals_against"] for g in games), "goals_for_per_game": sum(g["goals_for"] for g in games)/n if n else None, "goals_against_per_game": sum(g["goals_against"] for g in games)/n if n else None, "points_last5": sum(g["points"] for g in games[:5]), "points_last10": sum(g["points"] for g in recent), "congestion_14d": sum(0 <= (self.as_of-utc(g["kickoff"])).total_seconds()/86400 < 14 for g in games), "opponent_adjusted_performance": sum(g["elo_result"]-g["elo_expected_before"] for g in recent)/len(recent) if recent else None, "opponent_adjustment_notes": "最近十場實際 Elo 賽果分減賽前 Elo 期望分，已包含對手評分及主客場；勝／和／負分為 1／0.5／0。", "venue_stats": venue_stats, "form5": games[:5], "form10": recent, "home_record": [g for g in games if g["venue"] == "home"][:10], "away_record": [g for g in games if g["venue"] == "away"][:10], "rest_days": round((self.as_of-utc(games[0]["kickoff"])).total_seconds()/86400, 1) if games else None, "source": sorted({g["source"] for g in games if g.get("source")}), "as_of": iso(self.as_of), "xg": None, "xga": None, "injuries": None, "lineup": None}
