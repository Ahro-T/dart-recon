"""OpenDART client with on-disk cache. Standard library only."""

from __future__ import annotations

import io
import json
import os
import threading
import time
import urllib.parse
import http.client
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

BASE = "https://opendart.fss.or.kr/api"
ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "data" / "raw"

# reprt_code: 11011 사업보고서(연간), 11012 반기, 11013 1분기, 11014 3분기
ANNUAL = "11011"


def api_key() -> str:
    key = os.environ.get("DART_API_KEY")
    if key:
        return key
    env = ROOT / ".env"
    for line in env.read_text().splitlines():
        if line.startswith("DART_API_KEY="):
            return line.split("=", 1)[1].strip()
    raise RuntimeError("DART_API_KEY not set (.env or environment)")


class BlockedError(Exception):
    """연결 리셋·빈 응답이 연속 BREAKER_THRESHOLD회 -> 서버가 차단 중으로 보고 수집 전체를 멈춘다.

    RuntimeError가 아니므로 회사 단위 fetch_error 처리로 삼켜지지 않는다.
    """


BREAKER_THRESHOLD = 3
MIN_INTERVAL = max(1.0, float(os.environ.get("DART_MIN_INTERVAL", "1.0")))  # 요청 간 최소 간격(초), 1초 미만 불가

_lock = threading.Lock()
_consecutive_failures = 0
_tripped = False
_last_request = 0.0


def tripped() -> bool:
    return _tripped


def reset_breaker() -> None:
    global _consecutive_failures, _tripped, _last_request
    with _lock:
        _consecutive_failures, _tripped, _last_request = 0, False, 0.0


def _is_connection_failure(exc: BaseException) -> bool:
    return isinstance(exc, (ConnectionError, http.client.RemoteDisconnected, TimeoutError, urllib.error.URLError)) or (
        isinstance(exc, OSError) and not isinstance(exc, urllib.error.HTTPError)
    )


def _fetch(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=30) as resp:
        return resp.read()


def _get(path: str, params: dict) -> bytes:
    """직렬화된 단일 요청. 재시도 없음. 연속 실패가 임계치에 닿으면 BlockedError."""
    global _consecutive_failures, _tripped, _last_request
    query = urllib.parse.urlencode({"crtfc_key": api_key(), **params})
    with _lock:  # 락 안에서 간격 대기와 요청을 모두 수행 -> 스레드가 있어도 요청은 직렬, 간격 보장
        if _tripped:
            raise BlockedError("DART 차단 의심: 이전에 연속 실패로 수집 중단됨")
        wait = _last_request + MIN_INTERVAL - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        try:
            body = _fetch(f"{BASE}/{path}?{query}")
            if not body:
                raise ConnectionError("empty response")
        except Exception as exc:
            _last_request = time.monotonic()
            if not _is_connection_failure(exc):
                raise
            _consecutive_failures += 1
            if _consecutive_failures >= BREAKER_THRESHOLD:
                _tripped = True
                raise BlockedError(f"연결 리셋/빈 응답 {_consecutive_failures}회 연속: 수집 중단 ({exc})") from exc
            raise
        _last_request = time.monotonic()
        _consecutive_failures = 0
        return body


def listed_corps() -> list[dict]:
    """All corps with a stock code (listed), from corpCode.xml."""
    cache = CACHE / "corp_codes.json"
    if cache.exists():
        return json.loads(cache.read_text())
    raw = _get("corpCode.xml", {})
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        xml = zf.read(zf.namelist()[0])
    corps = []
    for node in ET.fromstring(xml).iter("list"):
        stock = (node.findtext("stock_code") or "").strip()
        if stock:
            corps.append(
                {
                    "corp_code": node.findtext("corp_code"),
                    "corp_name": node.findtext("corp_name"),
                    "stock_code": stock,
                }
            )
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(corps, ensure_ascii=False))
    return corps


def financials(corp_code: str, year: int, fs_div: str = "CFS", reprt_code: str = ANNUAL) -> dict:
    """Full financial statements (fnlttSinglAcntAll). Returns the raw API payload.

    status '000' = ok, '013' = no data. Cached per (corp, year, report, fs_div).
    """
    cache = CACHE / "fs" / f"{corp_code}_{year}_{reprt_code}_{fs_div}.json"
    if cache.exists():
        return json.loads(cache.read_text())
    if os.environ.get("DART_OFFLINE") == "1":
        raise RuntimeError(f"not cached (offline): {cache.name}")
    params = {"corp_code": corp_code, "bsns_year": str(year), "reprt_code": reprt_code, "fs_div": fs_div}
    payload = json.loads(_get("fnlttSinglAcntAll.json", params))  # 재시도 없음: 실패는 fetch_error, 연속 3회면 중단
    if payload.get("status") == "020":  # request limit exceeded
        raise RuntimeError("DART daily request limit exceeded")
    if payload.get("status") in ("000", "013"):
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(payload, ensure_ascii=False))
    return payload
