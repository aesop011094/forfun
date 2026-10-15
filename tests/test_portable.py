"""Portable-runtime prerequisites: refresh without Git and UTF-8 source caches."""
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from backend import providers


SHA = "a" * 40


def test_raw_cache_can_use_writable_user_storage(tmp_path):
    destination = tmp_path / "personal-data" / "raw"
    code = ("from backend import providers; from pathlib import Path; "
            f"assert providers.RAW_DIR==Path({str(destination)!r}); "
            "providers.RAW_DIR.mkdir(parents=True, exist_ok=True)")
    environment = dict(os.environ, FOOTBALL_RAW_DIR=str(destination))
    result = subprocess.run([sys.executable, "-c", code], env=environment,
                            cwd=Path(__file__).resolve().parents[1],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert destination.is_dir()


def without_git(monkeypatch, tmp_path, response):
    monkeypatch.delenv("OPENFOOTBALL_REF", raising=False)
    monkeypatch.setattr(providers, "RAW_DIR", tmp_path)

    async def missing_git(*args, **kwargs):
        raise FileNotFoundError("Git intentionally absent in portable runtime")

    monkeypatch.setattr(providers.asyncio, "create_subprocess_exec", missing_git)
    real_client = httpx.AsyncClient

    def handler(request):
        assert request.url.host == "api.github.com"
        assert request.url.path == "/repos/openfootball/football.json/commits"
        assert request.url.params["per_page"] == "1"
        assert request.headers["user-agent"] == "Football-Intelligence-AI/1.0"
        assert request.headers["x-github-api-version"] == "2022-11-28"
        return response

    def client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(providers.httpx, "AsyncClient", client)


def test_portable_sync_resolves_immutable_version_without_git(monkeypatch, tmp_path):
    without_git(monkeypatch, tmp_path, httpx.Response(200, json=[{"sha": SHA}]))
    assert asyncio.run(providers._resolve_openfootball_ref()) == (SHA, None)


@pytest.mark.parametrize("payload", [[{"sha": "master"}], [{"sha": 123}], [], {}])
def test_portable_sync_never_accepts_moving_or_malformed_reference(monkeypatch, tmp_path, payload):
    without_git(monkeypatch, tmp_path, httpx.Response(200, json=payload))
    with pytest.raises(ValueError, match="未能固定"):
        asyncio.run(providers._resolve_openfootball_ref())


def test_github_rate_limit_preserves_prior_pin_with_visible_staleness_warning(monkeypatch, tmp_path):
    without_git(monkeypatch, tmp_path, httpx.Response(403, json={"message": "rate limited"}))
    (tmp_path / "openfootball_pin.json").write_text(json.dumps({"commit": SHA}), encoding="utf-8")
    sha, warning = asyncio.run(providers._resolve_openfootball_ref())
    assert sha == SHA and "可能過時" in warning


def test_cache_utf8_independent_of_computer_locale(tmp_path):
    # A non-UTF-8 host default reproduces Windows cache problems without
    # depending on a Windows executable in the cloud build machine.
    code = (
        "from pathlib import Path; import hashlib; from backend import providers; "
        f"target=Path({str(tmp_path / 'fixture.json')!r}); "
        "raw=b'{\"matches\": []}'; "
        "meta={'sha256':hashlib.sha256(raw).hexdigest(),'warning':'\\u6578\\u64da\\u4e0d\\u8db3'}; "
        "providers._save_cache(target,raw,meta); "
        "assert providers._read_cache(target)==(raw,meta); "
        "assert b'\\xe6\\x95\\xb8' in target.with_suffix('.meta.json').read_bytes()"
    )
    environment = dict(os.environ, PYTHONUTF8="0", PYTHONCOERCECLOCALE="0", LC_ALL="C")
    result = subprocess.run([sys.executable, "-c", code], env=environment,
                            cwd=Path(__file__).resolve().parents[1],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
