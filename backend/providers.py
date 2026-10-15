"""Replaceable, provenance-preserving football data providers.

Only provider-published fixtures are accepted. Openfootball does not document
the timezone of its ``time`` field, so dates remain date-precision fixtures.
No score, kickoff time, lineup, injury, or xG is inferred by this module.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import tempfile
import unicodedata
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

UTC = timezone.utc
ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = Path(os.environ.get("FOOTBALL_RAW_DIR", ROOT / "data" / "raw"))
DEFAULT_SEASONS = ["2022-23", "2023-24", "2024-25", "2025-26", "2026-27"]
LEAGUES = {
    "PL": {"name": "英格蘭超級聯賽", "file": "en.1", "country": "England"},
    "PD": {"name": "西班牙甲組聯賽", "file": "es.1", "country": "Spain"},
    "BL1": {"name": "德國甲組聯賽", "file": "de.1", "country": "Germany"},
    "SA": {"name": "意大利甲組聯賽", "file": "it.1", "country": "Italy"},
    "FL1": {"name": "法國甲組聯賽", "file": "fr.1", "country": "France"},
}

# A provider change must not split one club's training history. Aliases are
# league-scoped; unrecognised names are preserved rather than guessed.
TEAM_ALIASES = {
    "PL": {
        "Arsenal": ["Arsenal FC"], "Aston Villa": ["Aston Villa FC"],
        "Bournemouth": ["AFC Bournemouth", "Bournemouth FC"],
        "Brentford": ["Brentford FC"],
        "Brighton & Hove Albion": ["Brighton & Hove Albion FC", "Brighton"],
        "Burnley": ["Burnley FC"], "Chelsea": ["Chelsea FC"],
        "Coventry City": ["Coventry City FC"],
        "Crystal Palace": ["Crystal Palace FC"], "Everton": ["Everton FC"],
        "Fulham": ["Fulham FC"], "Hull City": ["Hull City AFC"],
        "Ipswich Town": ["Ipswich Town FC"], "Leeds United": ["Leeds United FC"],
        "Leicester City": ["Leicester City FC"], "Liverpool": ["Liverpool FC"],
        "Luton Town": ["Luton Town FC"],
        "Manchester City": ["Manchester City FC"],
        "Manchester United": ["Manchester United FC", "Man United"],
        "Newcastle United": ["Newcastle United FC"],
        "Nottingham Forest": ["Nottingham Forest FC"],
        "Sheffield United": ["Sheffield United FC"],
        "Southampton": ["Southampton FC"], "Sunderland": ["Sunderland AFC"],
        "Tottenham Hotspur": ["Tottenham Hotspur FC", "Tottenham"],
        "West Ham United": ["West Ham United FC"],
        "Wolverhampton Wanderers": ["Wolverhampton Wanderers FC", "Wolves"],
    },
    "PD": {
        "Athletic Club": ["Athletic Club Bilbao", "Athletic Bilbao"],
        "Osasuna": ["CA Osasuna"], "Leganés": ["CD Leganés"],
        "Atlético Madrid": ["Club Atlético de Madrid", "Atlético de Madrid"],
        "Cádiz": ["Cádiz CF"], "Alavés": ["Deportivo Alavés"],
        "Elche": ["Elche CF"], "Barcelona": ["FC Barcelona"],
        "Getafe": ["Getafe CF"], "Girona": ["Girona FC"],
        "Granada": ["Granada CF"], "Levante": ["Levante UD"],
        "Málaga": ["Málaga CF"], "Celta Vigo": ["RC Celta de Vigo", "Celta de Vigo"],
        "Deportivo La Coruña": ["RC Deportivo La Coruña"],
        "Espanyol": ["RCD Espanyol de Barcelona", "RCD Espanyol"],
        "Mallorca": ["RCD Mallorca"],
        "Rayo Vallecano": ["Rayo Vallecano de Madrid"],
        "Real Betis": ["Real Betis Balompié"], "Real Madrid": ["Real Madrid CF"],
        "Real Oviedo": [],
        "Racing Santander": ["Real Racing Club de Santander", "Real Racing Club"],
        "Real Sociedad": ["Real Sociedad de Fútbol"],
        "Real Valladolid": ["Real Valladolid CF"],
        "Sevilla": ["Sevilla FC"], "Almería": ["UD Almería"],
        "Las Palmas": ["UD Las Palmas"], "Valencia": ["Valencia CF"],
        "Villarreal": ["Villarreal CF"],
    },
    "BL1": {
        "Heidenheim": ["1. FC Heidenheim 1846"], "Köln": ["1. FC Köln", "FC Köln"],
        "Union Berlin": ["1. FC Union Berlin"], "Mainz 05": ["1. FSV Mainz 05"],
        "Bayer Leverkusen": ["Bayer 04 Leverkusen"], "Borussia Dortmund": [],
        "Borussia Mönchengladbach": ["Bor. Mönchengladbach"],
        "Eintracht Frankfurt": [], "Augsburg": ["FC Augsburg"],
        "Bayern Munich": ["FC Bayern München", "Bayern München", "FC Bayern Munich"],
        "Schalke 04": ["FC Schalke 04"],
        "St. Pauli": ["FC St. Pauli 1910", "FC St. Pauli"],
        "Hamburger SV": [], "Hertha BSC": [], "Holstein Kiel": [],
        "RB Leipzig": [], "Freiburg": ["SC Freiburg"],
        "Paderborn": ["SC Paderborn 07"], "Elversberg": ["SV 07 Elversberg"],
        "Darmstadt": ["SV Darmstadt 98"], "Werder Bremen": ["SV Werder Bremen"],
        "Hoffenheim": ["TSG 1899 Hoffenheim"], "Stuttgart": ["VfB Stuttgart"],
        "Bochum": ["VfL Bochum 1848", "VfL Bochum"], "Wolfsburg": ["VfL Wolfsburg"],
    },
    "SA": {
        "AC Milan": [], "Monza": ["AC Monza"], "Pisa": ["AC Pisa 1909", "Pisa SC"],
        "Fiorentina": ["ACF Fiorentina"], "AS Roma": ["Roma"],
        "Atalanta": ["Atalanta BC"], "Bologna": ["Bologna FC 1909"],
        "Cagliari": ["Cagliari Calcio"], "Como": ["Como 1907"],
        "Empoli": ["Empoli FC"],
        "Inter Milan": ["FC Internazionale Milano", "Internazionale", "Inter"],
        "Frosinone": ["Frosinone Calcio"], "Genoa": ["Genoa CFC"],
        "Hellas Verona": ["Hellas Verona FC"], "Juventus": ["Juventus FC"],
        "Parma": ["Parma Calcio 1913"], "Lazio": ["SS Lazio"],
        "Napoli": ["SSC Napoli"], "Spezia": ["Spezia Calcio"],
        "Torino": ["Torino FC"], "Sampdoria": ["UC Sampdoria"],
        "Cremonese": ["US Cremonese"], "Lecce": ["US Lecce"],
        "Salernitana": ["US Salernitana 1919"], "Sassuolo": ["US Sassuolo Calcio"],
        "Udinese": ["Udinese Calcio"], "Venezia": ["Venezia FC"],
    },
    "FL1": {
        "Ajaccio": ["AC Ajaccio"], "Auxerre": ["AJ Auxerre"],
        "Monaco": ["AS Monaco", "AS Monaco FC"],
        "Saint-Étienne": ["AS Saint-Étienne"], "Angers": ["Angers SCO"],
        "Clermont": ["Clermont Foot 63"],
        "Troyes": ["ES Troyes AC", "ESTAC Troyes"],
        "Lorient": ["FC Lorient"], "Metz": ["FC Metz"], "Nantes": ["FC Nantes"],
        "Le Havre": ["Le Havre AC"], "Le Mans": ["Le Mans FC"],
        "Lille": ["Lille OSC"], "Montpellier": ["Montpellier HSC"],
        "Nice": ["OGC Nice"], "Lyon": ["Olympique Lyonnais"],
        "Marseille": ["Olympique Marseille", "Olympique de Marseille"],
        "Paris FC": [], "Paris Saint-Germain": ["Paris Saint-Germain FC", "PSG"],
        "Lens": ["RC Lens", "Racing Club de Lens"],
        "Strasbourg": ["RC Strasbourg", "RC Strasbourg Alsace"],
        "Brest": ["Stade Brestois 29"],
        "Rennes": ["Stade Rennais", "Stade Rennais FC 1901", "Stade Rennais FC"],
        "Reims": ["Stade de Reims"], "Toulouse": ["Toulouse FC"],
    },
}


def _name_key(name: str) -> str:
    plain = unicodedata.normalize("NFKD", name)
    plain = "".join(char for char in plain if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", " ", plain.lower()).strip()


_ALIAS_INDEX = {
    league: {_name_key(alias): canonical for canonical, aliases in clubs.items()
             for alias in [canonical, *aliases]}
    for league, clubs in TEAM_ALIASES.items()
}


def canonical_team(name: str, league: str) -> str:
    if not isinstance(name, str) or not name.strip() or len(name) > 150:
        raise ValueError("球隊名稱無效")
    clean = " ".join(name.split())
    return _ALIAS_INDEX.get(league, {}).get(_name_key(clean), clean)


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _match_id(league: str, season: str, home: str, away: str) -> str:
    # One league match per home/away pairing per season; no provider/date in ID.
    identity = json.dumps([league, season, home, away], ensure_ascii=False)
    return hashlib.sha256(identity.encode()).hexdigest()[:24]


def _score(value: Any) -> tuple[int | None, int | None]:
    if value is None:
        return None, None
    if isinstance(value, dict):
        value = value.get("ft")
        if value is None:
            return None, None
    if not isinstance(value, list) or len(value) != 2:
        raise ValueError("完場比分格式無效")
    if not all(type(goal) is int and 0 <= goal <= 30 for goal in value):
        raise ValueError("完場入球必須是合理的非負整數")
    return value[0], value[1]


def normalize_openfootball(
    payload: dict, league: str, season: str, source_url: str,
    fetched_at: str, *, now: datetime | None = None,
) -> tuple[list[dict], list[str]]:
    """Validate records individually, retaining useful rows after bad records."""
    now = now or _now()
    if league not in LEAGUES or not re.fullmatch(r"20\d{2}-\d{2}", season):
        raise ValueError("聯賽或球季無效")
    year = int(season[:4])
    if season[-2:] != str(year + 1)[-2:]:
        raise ValueError("球季年份不連續")
    if not isinstance(payload, dict):
        raise ValueError("數據必須是 JSON 物件")
    rows = payload.get("matches")
    if rows is None and isinstance(payload.get("rounds"), list):
        rows = [match for rnd in payload["rounds"] if isinstance(rnd, dict)
                for match in rnd.get("matches", [])]
    if not isinstance(rows, list) or len(rows) > 5000:
        raise ValueError("比賽清單格式或大小無效")
    matches, issues, seen = [], [], set()
    for index, row in enumerate(rows):
        try:
            if not isinstance(row, dict):
                raise ValueError("比賽必須是物件")
            day = date.fromisoformat(row.get("date", ""))
            if not date(year, 7, 1) <= day <= date(year + 1, 6, 30):
                raise ValueError("比賽日期超出該球季")
            home = canonical_team(row.get("team1"), league)
            away = canonical_team(row.get("team2"), league)
            if home == away:
                raise ValueError("主客隊不能相同")
            kickoff = datetime.combine(day, datetime.min.time(), UTC)
            home_goals, away_goals = _score(row.get("score"))
            # Date-only results from today have no verified finish timestamp.
            # Reject them until the following day, rather than infer kickoffs.
            if home_goals is not None and day >= now.date():
                raise ValueError("拒絕未來或今日未能確認完場時間的比分")
            identity = _match_id(league, season, home, away)
            if identity in seen:
                raise ValueError("同一球季主客隊組合重複")
            seen.add(identity)
            finished = home_goals is not None
            result_at = kickoff + timedelta(hours=36) if finished else None
            matches.append({
                "id": identity, "league": league, "season": season,
                "home": home, "away": away, "kickoff": _iso(kickoff),
                "kickoff_precision": "date", "fixture_date": day.isoformat(),
                "status": "FINISHED" if finished else "SCHEDULED",
                "home_goals": home_goals, "away_goals": away_goals,
                "source": "openfootball", "source_url": source_url,
                "fetched_at": fetched_at,
                "result_available_at": _iso(result_at) if result_at else None,
                "availability_basis": "retrospective_conservative_date_plus_36h",
                "official_verification": False,
                "round": str(row.get("round", ""))[:100],
                "source_time_unverified": str(row.get("time", ""))[:10] or None,
                "crests": {"home": None, "away": None},
            })
        except (ValueError, TypeError, AttributeError) as exc:
            # Exception messages here are our own validation messages, no secrets.
            issues.append(f"{league} {season} 第 {index + 1} 筆：{str(exc)[:160]}")
    return matches, issues


async def _resolve_openfootball_ref() -> tuple[str, str | None]:
    configured = os.environ.get("OPENFOOTBALL_REF", "").strip()
    if configured:
        if re.fullmatch(r"[0-9a-f]{40}", configured):
            return configured, None
        raise ValueError("OPENFOOTBALL_REF 必須是完整 40 字元 Git commit SHA")
    try:
        process = await asyncio.create_subprocess_exec(
            "git", "ls-remote", "https://github.com/openfootball/football.json", "HEAD",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            output, _ = await asyncio.wait_for(process.communicate(), timeout=25)
        except asyncio.TimeoutError:
            process.kill()
            await process.communicate()
            raise ValueError("Git 來源版本查詢逾時")
        sha = output.decode().split()[0] if output else ""
        if process.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}", sha):
            return sha, None
    except (OSError, ValueError):
        pass
    # Portable Windows downloads intentionally do not require a Git install.
    # GitHub's public API resolves the default branch to one immutable commit;
    # every dataset request below then uses that SHA, never a moving branch.
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10), follow_redirects=True) as client:
            raw, _ = await _fetch(
                client,
                "https://api.github.com/repos/openfootball/football.json/commits?per_page=1",
                headers={"Accept": "application/vnd.github+json",
                         "User-Agent": "Football-Intelligence-AI/1.0",
                         "X-GitHub-Api-Version": "2022-11-28"},
                attempts=2,
            )
        commits = json.loads(raw)
        sha = commits[0].get("sha", "") if isinstance(commits, list) and commits else ""
        if isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{40}", sha):
            return sha, None
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    # A prior immutable pin is preferable to silently mixing master versions.
    pin_path = RAW_DIR / "openfootball_pin.json"
    if pin_path.exists():
        try:
            pin = json.loads(pin_path.read_text(encoding="utf-8"))
            sha = pin["commit"]
            if re.fullmatch(r"[0-9a-f]{40}", sha):
                return sha, "未能查詢最新版本；使用上次已固定的來源版本，可能過時。"
        except (ValueError, KeyError, TypeError):
            pass
    raise ValueError("未能固定 Openfootball 來源版本；請稍後重試，沒有使用不確定版本")


def _read_cache(cache_path: Path) -> tuple[bytes, dict] | None:
    try:
        meta = json.loads(cache_path.with_suffix(".meta.json").read_text(encoding="utf-8"))
        raw = cache_path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != meta.get("sha256"):
            return None
        return raw, meta
    except (OSError, ValueError, TypeError):
        return None


def _save_cache(cache_path: Path, raw: bytes, metadata: dict) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    # Unique temporary files tolerate two sync jobs fetching the same pin.
    with tempfile.NamedTemporaryFile(dir=cache_path.parent, suffix=".download", delete=False) as handle:
        handle.write(raw)
        temporary = Path(handle.name)
    temporary.replace(cache_path)
    meta_path = cache_path.with_suffix(".meta.json")
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=cache_path.parent, suffix=".download", delete=False) as handle:
        handle.write(json.dumps(metadata, ensure_ascii=False, indent=2))
        temporary_meta = Path(handle.name)
    temporary_meta.replace(meta_path)


async def _fetch(
    client: httpx.AsyncClient, url: str, *, headers: dict | None = None,
    attempts: int = 3, before_attempt: Any = None,
) -> tuple[bytes, dict]:
    """Bounded requests, certificate verification enabled by httpx default."""
    for attempt in range(attempts):
        if before_attempt is not None:
            await before_attempt()
        try:
            response = await client.get(url, headers=headers)
        except (httpx.TimeoutException, httpx.TransportError):
            if attempt == attempts - 1:
                raise ValueError("來源連線失敗或逾時；保留已有資料，沒有生成替代賽程")
            await asyncio.sleep(2 ** attempt)
            continue
        if response.status_code == 200:
            raw = response.content
            if len(raw) > 8_000_000:
                raise ValueError("來源回應超過 8 MB 上限")
            return raw, {
                "etag": response.headers.get("etag"),
                "last_modified": response.headers.get("last-modified"),
            }
        if response.status_code in {429, 500, 502, 503, 504} and attempt < attempts - 1:
            retry = response.headers.get("retry-after", "")
            delay = min(max(float(retry), 1), 15) if retry.isdigit() else 2 ** attempt
            await asyncio.sleep(delay)
            continue
        if response.status_code == 401:
            raise ValueError("API 驗證失敗；請在環境 Secrets 檢查金鑰，勿把值貼到聊天")
        if response.status_code == 403:
            raise ValueError("來源拒絕存取；檢查網絡允許清單或帳戶方案的聯賽權限")
        if response.status_code == 429:
            raise ValueError("來源請求額度已達上限；有限次重試後暫停")
        raise ValueError(f"來源回傳 HTTP {response.status_code}，沒有生成替代資料")
    raise ValueError("來源重試次數已用盡")


async def sync_openfootball(seasons: list[str], leagues: list[str]) -> dict:
    started = _now()
    result = {"matches": [], "errors": [], "sources": [], "fetched_at": _iso(started)}
    invalid = [league for league in leagues if league not in LEAGUES]
    if invalid:
        result["errors"].append("不支援的聯賽代碼")
    seasons = list(dict.fromkeys(seasons))
    if len(seasons) > 15 or any(not re.fullmatch(r"20\d{2}-\d{2}", str(s)) for s in seasons):
        result["errors"].append("球季格式無效或超過 15 季上限")
        return result
    try:
        commit, pin_warning = await _resolve_openfootball_ref()
    except ValueError as exc:
        result["errors"].append(str(exc))
        return result
    result["commit"] = commit
    if pin_warning:
        result["errors"].append(pin_warning)
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    (RAW_DIR / "openfootball_pin.json").write_text(json.dumps({"commit": commit, "resolved_at": _iso(started)}), encoding="utf-8")
    semaphore = asyncio.Semaphore(4)
    async with httpx.AsyncClient(timeout=httpx.Timeout(25), follow_redirects=True) as client:
        async def dataset(season: str, league: str) -> dict:
            url = f"https://raw.githubusercontent.com/openfootball/football.json/{commit}/{season}/{LEAGUES[league]['file']}.json"
            cache_path = RAW_DIR / f"openfootball-{commit}-{season}-{league}.json"
            metadata = {"name": "openfootball", "league": league, "season": season,
                        "url": url, "commit": commit, "license": "CC0-1.0",
                        "official_verification": False, "kickoff_precision": "date"}
            try:
                async with semaphore:
                    cached = await asyncio.to_thread(_read_cache, cache_path)
                    if cached:
                        raw, cached_meta = cached
                        metadata.update(cached_meta)
                        metadata["cache_hit"] = True
                    else:
                        raw, response_meta = await _fetch(client, url)
                        metadata.update(response_meta)
                        metadata.update({"sha256": hashlib.sha256(raw).hexdigest(),
                                         "fetched_at": _iso(_now()), "cache_hit": False})
                        # Raw provenance saved before normalization, including bad rows.
                        await asyncio.to_thread(_save_cache, cache_path, raw, metadata)
                payload = json.loads(raw)
                matches, issues = normalize_openfootball(payload, league, season, url,
                                                         metadata["fetched_at"], now=started)
                for match in matches:
                    match["source_commit"] = commit
                    match["source_sha256"] = metadata["sha256"]
                finished = [m for m in matches if m["status"] == "FINISHED"]
                missing_past = sum(m["status"] != "FINISHED" and m["kickoff"][:10] < started.date().isoformat()
                                   for m in matches)
                latest = max((m["kickoff"] for m in finished), default=None)
                metadata.update({"accepted": len(matches), "finished": len(finished),
                                 "rejected": len(issues), "latest_result_at": latest,
                                 "missing_past_results": missing_past,
                                 "last_result_at": latest,
                                 "stale_days": (started.date() - date.fromisoformat(latest[:10])).days if latest else None,
                                 "checked_at": _iso(started),
                                 "warning": "社群公開數據；未經官方逐場核實，時間時區未確認；上游由人手更新，賽果可能滯後。",
                                 "status": "partial" if issues or missing_past else "ok"})
                return {"matches": matches, "issues": issues, "metadata": metadata}
            except (ValueError, TypeError, KeyError, OSError) as exc:
                # Never expose arbitrary exception reprs from credentialed clients.
                metadata.update({"status": "error", "checked_at": _iso(started)})
                message = str(exc)[:180] if isinstance(exc, ValueError) else "資料格式或本地來源快取失敗"
                return {"matches": [], "issues": [f"{league} {season}：{message}"], "metadata": metadata}
        batches = await asyncio.gather(*(dataset(season, league) for season in seasons
                                       for league in dict.fromkeys(leagues) if league in LEAGUES))
    for batch in batches:
        result["matches"].extend(batch["matches"])
        result["errors"].extend(batch["issues"])
        result["sources"].append(batch["metadata"])
    result["errors"] = result["errors"][:100]
    result["counts"] = {"matches": len(result["matches"]),
                        "finished": sum(m["status"] == "FINISHED" for m in result["matches"])}
    current_sources = [s for s in result["sources"] if s["season"] == max(seasons, default="")]
    result["last_result_at"] = max((s.get("last_result_at") or "" for s in current_sources), default="") or None
    result["warnings"] = ["Openfootball 是 CC0 社群研究來源，並非官方即時 API；所有預測屬實驗性研究。",
                          "歷史比分檔案沒有當時的發布時間；回測採用日期後 36 小時的保守假設，仍不能證明資料當時已發布。"]
    if result["last_result_at"]:
        stale = (started.date() - date.fromisoformat(result["last_result_at"][:10])).days
        if stale > 7:
            result["warnings"].append(f"本球季最新已知賽果為 {result['last_result_at'][:10]}，已滯後 {stale} 日；近期狀態可能不完整。")
    return result


def normalize_football_data(payload: dict, league: str, source_url: str, fetched_at: str,
                            *, now: datetime | None = None) -> tuple[list[dict], list[str]]:
    now = now or _now()
    if not isinstance(payload, dict) or not isinstance(payload.get("matches"), list):
        raise ValueError("API 比賽清單格式無效")
    matches, issues, seen = [], [], set()
    for index, row in enumerate(payload["matches"]):
        try:
            if not isinstance(row, dict):
                raise ValueError("比賽必須是物件")
            competition = row.get("competition", {}).get("code")
            if competition and competition != league:
                raise ValueError("API 比賽聯賽不符")
            moment = datetime.fromisoformat(row["utcDate"].replace("Z", "+00:00"))
            if moment.tzinfo is None:
                raise ValueError("API 開賽時間缺少時區")
            kickoff = moment.astimezone(UTC)
            start = date.fromisoformat(row["season"]["startDate"])
            year = start.year if start.month >= 7 else start.year - 1
            season = f"{year}-{str(year + 1)[-2:]}"
            home = canonical_team(row["homeTeam"]["name"], league)
            away = canonical_team(row["awayTeam"]["name"], league)
            if home == away:
                raise ValueError("主客隊不能相同")
            status = row.get("status")
            if status == "FINISHED":
                # League matches only; full-time normally means 90 minutes.
                score = row.get("score", {})
                if score.get("duration", "REGULAR") != "REGULAR":
                    raise ValueError("拒絕非正常 90 分鐘聯賽比分")
                ft = score.get("fullTime", {})
                goals = [ft.get("home"), ft.get("away")]
                home_goals, away_goals = _score(goals)
                if kickoff + timedelta(hours=2) > now:
                    raise ValueError("拒絕未來或未合理完場的 API 比分")
            else:
                home_goals, away_goals = None, None
                if status in {"IN_PLAY", "PAUSED", "LIVE"}:
                    # Live predictions are outside this application's scope.
                    status = "IN_PLAY"
                elif status in {"POSTPONED", "SUSPENDED", "CANCELLED"}:
                    status = "POSTPONED"
                else:
                    status = "SCHEDULED"
            identity = _match_id(league, season, home, away)
            if identity in seen:
                raise ValueError("重複比賽")
            seen.add(identity)
            # This connector is prospective: its result was known at fetch time,
            # never falsely claimed available historically.
            result_at = max(kickoff + timedelta(hours=3),
                            datetime.fromisoformat(fetched_at.replace("Z", "+00:00"))) if home_goals is not None else None
            def crest(team: dict) -> str | None:
                url = team.get("crest")
                return url if isinstance(url, str) and url.startswith("https://") else None
            matches.append({
                "id": identity, "league": league, "season": season,
                "home": home, "away": away, "kickoff": _iso(kickoff),
                "kickoff_precision": "exact", "fixture_date": kickoff.date().isoformat(),
                "status": status, "home_goals": home_goals, "away_goals": away_goals,
                "source": "football-data.org", "source_url": source_url,
                "fetched_at": fetched_at, "result_available_at": _iso(result_at) if result_at else None,
                "availability_basis": "observed_at_fetch", "official_verification": True,
                "provider_match_id": row.get("id"),
                "crests": {"home": crest(row["homeTeam"]), "away": crest(row["awayTeam"])},
            })
        except (ValueError, TypeError, KeyError, AttributeError):
            issues.append(f"{league} API 第 {index + 1} 筆資料不符時間、比分或格式要求，已排除")
    return matches, issues


async def sync_football_data(api_key: str, leagues: list[str]) -> dict:
    started = _now()
    result = {"matches": [], "errors": [], "sources": [], "fetched_at": _iso(started)}
    if not api_key or not api_key.strip():
        result["errors"].append("未設定 FOOTBALL_DATA_API_KEY；請在環境 Secrets 設定，勿把值貼到聊天")
        return result
    if os.environ.get("FOOTBALL_DATA_TERMS_ACCEPTED", "false").lower() != "true":
        result["errors"].append("尚未確認 football-data.org 帳戶方案及條款允許目前用途；請先確認再設定 FOOTBALL_DATA_TERMS_ACCEPTED=true")
        return result
    next_request = 0.0
    async def pace() -> None:
        nonlocal next_request
        loop = asyncio.get_running_loop()
        delay = max(0.0, next_request - loop.time())
        if delay:
            await asyncio.sleep(delay)
        # Application's own conservative throttle, not a contractual promise.
        next_request = loop.time() + 7.0
    date_from = (started.date() - timedelta(days=60)).isoformat()
    date_to = (started.date() + timedelta(days=14)).isoformat()
    async with httpx.AsyncClient(timeout=httpx.Timeout(25), follow_redirects=False) as client:
        for league in dict.fromkeys(leagues):
            if league not in LEAGUES:
                result["errors"].append("不支援的聯賽代碼")
                continue
            url = f"https://api.football-data.org/v4/competitions/{league}/matches?dateFrom={date_from}&dateTo={date_to}"
            metadata = {"name": "football-data.org", "league": league, "url": url,
                        "fetched_at": _iso(_now()), "kickoff_precision": "exact"}
            try:
                raw, response_meta = await _fetch(client, url, headers={"X-Auth-Token": api_key.strip()},
                                                 before_attempt=pace)
                fetched_at = _iso(_now())
                metadata.update(response_meta)
                metadata.update({"fetched_at": fetched_at, "sha256": hashlib.sha256(raw).hexdigest()})
                # Credentials never appear in URL, raw payload or sidecar.
                path = RAW_DIR / f"football-data-{league}-{started.strftime('%Y%m%dT%H%M%S%f')}.json"
                await asyncio.to_thread(_save_cache, path, raw, metadata)
                matches, issues = normalize_football_data(json.loads(raw), league, url, fetched_at, now=_now())
                for match in matches:
                    match["source_sha256"] = metadata["sha256"]
                result["matches"].extend(matches)
                result["errors"].extend(issues)
                latest = max((m["kickoff"] for m in matches if m["status"] == "FINISHED"), default=None)
                metadata.update({"status": "partial" if issues else "ok", "accepted": len(matches),
                                 "rejected": len(issues), "last_result_at": latest,
                                 "latest_result_at": latest})
            except (ValueError, OSError, TypeError):
                # Never relay exception text from credentialed requests.
                result["errors"].append(f"{league} football-data.org 同步失敗：連線、額度、權限或資料格式問題；保留原有資料，請檢查來源狀態")
                metadata.update({"status": "error"})
            result["sources"].append(metadata)
    result["counts"] = {"matches": len(result["matches"]),
                        "finished": sum(m["status"] == "FINISHED" for m in result["matches"])}
    result["warnings"] = ["API 只同步最近 60 日及未來 14 日；歷史訓練依然採用已註明限制的 CC0 來源。",
                          "目前未接入真實 xG、xGA、傷停或正選資料。"]
    return result
