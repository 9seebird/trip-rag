"""날씨 — 기상청 단기예보 (공공데이터포털 VilageFcstInfoService_2.0 / getVilageFcst).

검색 결과·코스의 위치로 '앞으로 12시간' 의 비 소식을 요약한다.
화면은 비 예보가 있으면 "실내 위주로 다시 찾아볼까요?" 를 띄운다 (추천을 몰래 바꾸지 않고 제안만).

키: 공공데이터포털 키 하나로 여러 API 를 쓴다. 다만 API 마다 **활용신청**을 따로 해야 한다.
    WEATHER_API_KEY 가 없으면 TOUR_API_KEY 를 쓴다. 신청 전이면 ok=False 로 조용히 빠진다.

좌표 → 기상청 격자(nx, ny)는 기상청이 공개한 람베르트 정각원추도법 변환식 그대로.
"""
import math
import os
import time
import urllib.parse
from datetime import datetime, timedelta, timezone

import httpx

from ingest.db import log_call
from . import cache

KST = timezone(timedelta(hours=9))
URL = "https://apis.data.go.kr/1360000/VilageFcstInfoService_2.0/getVilageFcst"
BASE_HOURS = (2, 5, 8, 11, 14, 17, 20, 23)       # 발표 시각. 각 시각 +10분쯤부터 받을 수 있다
PTY_NAME = {"1": "비", "2": "비/눈", "3": "눈", "4": "소나기"}
SKY_NAME = {"1": "맑음", "3": "구름많음", "4": "흐림"}


def _key():
    k = (os.getenv("WEATHER_API_KEY") or os.getenv("TOUR_API_KEY") or "").strip()
    return urllib.parse.unquote(k) if "%" in k else k


def to_grid(lat, lon):
    """위경도 → 기상청 동네예보 격자 (기상청 공개 변환식)."""
    RE, GRID, SLAT1, SLAT2, OLON, OLAT, XO, YO = 6371.00877, 5.0, 30.0, 60.0, 126.0, 38.0, 43, 136
    D = math.pi / 180.0
    re_ = RE / GRID
    s1, s2, olon, olat = SLAT1 * D, SLAT2 * D, OLON * D, OLAT * D
    sn = math.log(math.cos(s1) / math.cos(s2)) / math.log(
        math.tan(math.pi * 0.25 + s2 * 0.5) / math.tan(math.pi * 0.25 + s1 * 0.5))
    sf = math.pow(math.tan(math.pi * 0.25 + s1 * 0.5), sn) * math.cos(s1) / sn
    ro = re_ * sf / math.pow(math.tan(math.pi * 0.25 + olat * 0.5), sn)
    ra = re_ * sf / math.pow(math.tan(math.pi * 0.25 + lat * D * 0.5), sn)
    theta = lon * D - olon
    if theta > math.pi:
        theta -= 2 * math.pi
    if theta < -math.pi:
        theta += 2 * math.pi
    theta *= sn
    return int(ra * math.sin(theta) + XO + 0.5), int(ro - ra * math.cos(theta) + YO + 0.5)


def base_time(now=None):
    """지금 받을 수 있는 가장 최근 발표 (base_date, base_time)."""
    now = (now or datetime.now(KST)) - timedelta(minutes=15)
    hours = [h for h in BASE_HOURS if h <= now.hour]
    if hours:
        return now.strftime("%Y%m%d"), f"{hours[-1]:02d}00"
    y = now - timedelta(days=1)
    return y.strftime("%Y%m%d"), "2300"


def summarize(items, now=None, hours=12):
    """예보 항목 → 앞으로 hours 시간 요약."""
    now = now or datetime.now(KST)
    end = now + timedelta(hours=hours)
    slots = {}
    for it in items:
        try:
            t = datetime.strptime(it["fcstDate"] + it["fcstTime"], "%Y%m%d%H%M").replace(tzinfo=KST)
        except (KeyError, ValueError):
            continue
        if now - timedelta(hours=1) < t <= end:
            slots.setdefault(t, {})[it.get("category")] = it.get("fcstValue")
    if not slots:
        return None
    pops = [int(v.get("POP", 0) or 0) for v in slots.values()]
    temps = [float(v["TMP"]) for v in slots.values() if v.get("TMP") not in (None, "")]
    rain_at = [t for t, v in sorted(slots.items()) if v.get("PTY", "0") not in ("0", "", None)]
    pty = next((PTY_NAME.get(slots[t]["PTY"]) for t in rain_at), None)
    first = slots[min(slots)]
    return {
        "rain": bool(rain_at) or max(pops) >= 60,
        "pop_max": max(pops),
        "rain_from": rain_at[0].strftime("%H시") if rain_at else None,
        "rain_kind": pty,
        "sky": SKY_NAME.get(first.get("SKY", ""), None),
        "tmp_min": round(min(temps)) if temps else None,
        "tmp_max": round(max(temps)) if temps else None,
    }


def forecast(conn, lat, lon):
    nx, ny = to_grid(lat, lon)
    bd, bt = base_time()
    key = cache.make_key("weather", nx=nx, ny=ny, bd=bd, bt=bt)
    hit = cache.get(conn, "weather", key)
    if hit:
        return hit
    if not _key():
        return {"ok": False, "reason": "키 없음"}
    t0 = time.time()
    try:
        r = httpx.get(URL, params={"serviceKey": _key(), "pageNo": 1, "numOfRows": 400, "dataType": "JSON",
                                    "base_date": bd, "base_time": bt, "nx": nx, "ny": ny}, timeout=8)
        ms = int((time.time() - t0) * 1000)
        body = r.json() if "json" in r.headers.get("content-type", "") else None
        head = ((body or {}).get("response") or {}).get("header") or {}
        if r.status_code != 200 or head.get("resultCode") != "00":
            note = head.get("resultMsg") or r.text[:200]
            log_call(conn, "kma", "getVilageFcst", False, r.status_code, ms, note=note)
            # 활용신청 전이면 SERVICE_KEY_IS_NOT_REGISTERED / 403 이 온다 → 화면은 날씨 없이
            return {"ok": False, "reason": note[:80]}
        log_call(conn, "kma", "getVilageFcst", True, 200, ms)
        items = body["response"]["body"]["items"]["item"]
    except Exception as e:
        log_call(conn, "kma", "getVilageFcst", False, None, int((time.time() - t0) * 1000), note=str(e)[:200])
        return {"ok": False, "reason": "기상청 응답 오류"}
    s = summarize(items)
    out = {"ok": bool(s), **(s or {}), "grid": [nx, ny], "base": f"{bd} {bt}"}
    if s:
        cache.put(conn, "weather", key, out)
    return out
