"""OpenDART client with on-disk cache. Standard library only."""

from __future__ import annotations

import io
import json
import os
import time
import urllib.parse
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


def _get(path: str, params: dict) -> bytes:
    query = urllib.parse.urlencode({"crtfc_key": api_key(), **params})
    with urllib.request.urlopen(f"{BASE}/{path}?{query}", timeout=30) as resp:
        return resp.read()


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
    for attempt in range(6):
        try:
            payload = json.loads(_get("fnlttSinglAcntAll.json", params))
            break
        except (OSError, ValueError):
            if attempt == 5:
                raise
            time.sleep(2 ** attempt)
    if payload.get("status") == "020":  # request limit exceeded
        raise RuntimeError("DART daily request limit exceeded")
    if payload.get("status") in ("000", "013"):
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(payload, ensure_ascii=False))
    return payload
