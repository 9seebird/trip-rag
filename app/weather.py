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


# ── 날씨 창: 오늘~10일 (단기예보 + 중기예보) ─────────────────────────────
# 0~3일 뒤는 단기예보(동네 격자), 4~10일 뒤는 중기예보(광역 육상예보 + 도시 기온).
# 중기예보(MidFcstInfoService)는 활용신청을 따로 해야 한다. 신청 전이면 단기예보 3~4일치만 나온다.
MID_LAND = "https://apis.data.go.kr/1360000/MidFcstInfoService/getMidLandFcst"
MID_TA = "https://apis.data.go.kr/1360000/MidFcstInfoService/getMidTa"
LAND_NAME = {"11B00000": "서울·인천·경기", "11D10000": "강원 영서", "11D20000": "강원 영동",
             "11C20000": "대전·세종·충남", "11C10000": "충북", "11F20000": "광주·전남", "11F10000": "전북",
             "11H10000": "대구·경북", "11H20000": "부산·울산·경남", "11G00000": "제주"}
# 중기 기온 도시 (기온 구역코드, 이름, 위도, 경도, 육상예보 구역)
MID_CITIES = [
    ("11B10101", "서울", 37.566, 126.978, "11B00000"), ("11B20201", "인천", 37.456, 126.705, "11B00000"),
    ("11B20601", "수원", 37.264, 127.029, "11B00000"), ("11B20305", "파주", 37.760, 126.780, "11B00000"),
    ("11B20701", "이천", 37.272, 127.435, "11B00000"), ("11B20606", "평택", 36.992, 127.113, "11B00000"),
    ("11D10301", "춘천", 37.881, 127.730, "11D10000"), ("11D10401", "원주", 37.342, 127.920, "11D10000"),
    ("11D20501", "강릉", 37.752, 128.876, "11D20000"), ("11D20401", "속초", 38.207, 128.592, "11D20000"),
    ("11C20401", "대전", 36.350, 127.385, "11C20000"), ("11C20404", "세종", 36.480, 127.289, "11C20000"),
    ("11C20104", "홍성", 36.601, 126.660, "11C20000"), ("11C20301", "천안", 36.815, 127.114, "11C20000"),
    ("11C20201", "보령", 36.333, 126.613, "11C20000"),
    ("11C10301", "청주", 36.642, 127.489, "11C10000"), ("11C10101", "충주", 36.991, 127.926, "11C10000"),
    ("11F10201", "전주", 35.824, 127.148, "11F10000"), ("21F10501", "군산", 35.968, 126.737, "11F10000"),
    ("11F20501", "광주", 35.160, 126.853, "11F20000"), ("21F20801", "목포", 34.812, 126.392, "11F20000"),
    ("11F20401", "여수", 34.760, 127.662, "11F20000"), ("11F20603", "순천", 34.951, 127.487, "11F20000"),
    ("11H10701", "대구", 35.871, 128.601, "11H10000"), ("11H10501", "안동", 36.568, 128.729, "11H10000"),
    ("11H10201", "포항", 36.019, 129.343, "11H10000"),
    ("11H20201", "부산", 35.180, 129.076, "11H20000"), ("11H20101", "울산", 35.538, 129.311, "11H20000"),
    ("11H20301", "창원", 35.228, 128.681, "11H20000"), ("11H20701", "진주", 35.180, 128.108, "11H20000"),
    ("11H20401", "통영", 34.854, 128.433, "11H20000"),
    ("11G00201", "제주", 33.500, 126.531, "11G00000"), ("11G00401", "서귀포", 33.254, 126.560, "11G00000"),
]
DOW_KO = "월화수목금토일"


def _near_cities(lat, lon):
    d = lambda c: (c[2] - lat) ** 2 + ((c[3] - lon) * math.cos(math.radians(lat))) ** 2
    return sorted(MID_CITIES, key=d)


def _call(conn, url, op, params):
    """공공데이터포털 JSON 호출 → item 목록 (실패면 None)."""
    t0 = time.time()
    try:
        r = httpx.get(url, params={"serviceKey": _key(), "pageNo": 1, "dataType": "JSON", **params}, timeout=8)
        ms = int((time.time() - t0) * 1000)
        body = r.json() if "json" in r.headers.get("content-type", "") else None
        head = ((body or {}).get("response") or {}).get("header") or {}
        if r.status_code != 200 or head.get("resultCode") != "00":
            log_call(conn, "kma", op, False, r.status_code, ms, note=(head.get("resultMsg") or r.text[:200]))
            return None
        log_call(conn, "kma", op, True, 200, ms)
        items = body["response"]["body"]["items"]["item"]
        return items if isinstance(items, list) else [items]
    except Exception as e:
        log_call(conn, "kma", op, False, None, int((time.time() - t0) * 1000), note=str(e)[:200])
        return None


def _wf_of(sky, pty):
    if pty and pty != "0":
        return PTY_NAME.get(pty, "비")
    return SKY_NAME.get(sky or "", None)


def _short_days(items, now):
    """단기예보 항목 → {날짜: {am, pm, tmin, tmax}} 와 지금 날씨."""
    slots = {}
    for it in items:
        slots.setdefault(it["fcstDate"] + it["fcstTime"], {})[it.get("category")] = it.get("fcstValue")
    days, hours = {}, []
    for k in sorted(slots):
        v = slots[k]
        if v.get("TMP") not in (None, ""):
            hours.append({"t": k, "tmp": round(_num(v["TMP"])), "wf": _wf_of(v.get("SKY"), v.get("PTY")),
                          "pop": int(_num(v.get("POP")) or 0), "reh": _num(v.get("REH")), "wsd": _num(v.get("WSD"))})
        date, hh = k[:8], int(k[8:10])
        d = days.setdefault(date, {"temps": [], "am": [], "pm": [], "tmin": None, "tmax": None})
        if v.get("TMN") not in (None, ""):
            d["tmin"] = _num(v["TMN"])
        if v.get("TMX") not in (None, ""):
            d["tmax"] = _num(v["TMX"])
        if v.get("TMP") not in (None, ""):
            d["temps"].append(_num(v["TMP"]))
        if 6 <= hh < 12:
            d["am"].append(v)
        elif 12 <= hh < 21:
            d["pm"].append(v)

    def half(vs):
        if not vs:
            return None
        pop = max(int(_num(x.get("POP")) or 0) for x in vs)
        rain = [x for x in vs if x.get("PTY", "0") not in ("0", "", None)]
        if rain:
            wf = _wf_of(None, rain[0]["PTY"])
        else:
            skies = [x.get("SKY") for x in vs if x.get("SKY")]
            wf = SKY_NAME.get(max(set(skies), key=skies.count), None) if skies else None
            if pop >= 60:
                wf = f"{wf or '흐림'}, 비 가능"
        return {"wf": wf, "pop": pop}

    out = {}
    for date, d in days.items():
        tmin = d["tmin"] if d["tmin"] is not None else (min(d["temps"]) if d["temps"] else None)
        tmax = d["tmax"] if d["tmax"] is not None else (max(d["temps"]) if d["temps"] else None)
        # 발표 시각에 따라 맨 끝 날은 새벽 몇 시간만 들어온다 → 그런 날의 최저·최고는 믿지 않는다
        enough = (d["tmin"] is not None or d["tmax"] is not None or len(d["temps"]) >= 18
                  or date == min(days))                          # 오늘은 남은 시간만이라도 보여 준다
        out[date] = {"am": half(d["am"]), "pm": half(d["pm"]),
                     "tmin": round(tmin) if tmin is not None and enough else None,
                     "tmax": round(tmax) if tmax is not None and enough else None}
    return out, hours


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def mid_tmfc(now=None):
    """중기예보 발표시각 (06·18시, 30분 뒤부터 받는다)."""
    now = (now or datetime.now(KST)) - timedelta(minutes=40)
    if now.hour >= 18:
        return now.strftime("%Y%m%d") + "1800"
    if now.hour >= 6:
        return now.strftime("%Y%m%d") + "0600"
    return (now - timedelta(days=1)).strftime("%Y%m%d") + "1800"


def _mid(conn, lat, lon):
    """4~10일 뒤: {날짜: {am, pm, tmin, tmax}}, 쓰인 구역 이름."""
    tm = mid_tmfc()
    cities = _near_cities(lat, lon)
    land_id = cities[0][4]
    key = cache.make_key("wx_mid", land=land_id, city=cities[0][0], tm=tm)
    hit = cache.get(conn, "weather", key)
    if hit:
        return hit
    land = _call(conn, MID_LAND, "getMidLandFcst", {"numOfRows": 10, "regId": land_id, "tmFc": tm})
    ta, city = None, None
    for c in cities[:3]:                     # 혹시 도시 코드가 비면 다음으로 가까운 도시
        ta = _call(conn, MID_TA, "getMidTa", {"numOfRows": 10, "regId": c[0], "tmFc": tm})
        if ta:
            city = c
            break
    if not land and not ta:
        return None
    L, T = (land or [{}])[0], (ta or [{}])[0]
    base = datetime.strptime(tm[:8], "%Y%m%d")
    days = {}
    for n in range(3, 11):
        date = (base + timedelta(days=n)).strftime("%Y%m%d")
        if f"wf{n}Am" in L:
            am = {"wf": L.get(f"wf{n}Am") or None, "pop": _num(L.get(f"rnSt{n}Am"))}
            pm = {"wf": L.get(f"wf{n}Pm") or None, "pop": _num(L.get(f"rnSt{n}Pm"))}
        elif f"wf{n}" in L:
            am = pm = {"wf": L.get(f"wf{n}") or None, "pop": _num(L.get(f"rnSt{n}"))}
        else:
            am = pm = None
        tmin, tmax = _num(T.get(f"taMin{n}")), _num(T.get(f"taMax{n}"))
        if not (am and am["wf"]) and tmin is None:
            continue
        days[date] = {"am": am if am and am["wf"] else None, "pm": pm if pm and pm["wf"] else None,
                      "tmin": round(tmin) if tmin is not None else None,
                      "tmax": round(tmax) if tmax is not None else None}
    out = {"days": days, "land": LAND_NAME.get(land_id), "city": city[1] if city else None, "tm": tm}
    cache.put(conn, "weather", key, out)
    return out


def week(conn, lat, lon):
    """날씨 창: 지금 + 오늘부터 10일 (날짜순, 단기 우선)."""
    if not _key():
        return {"ok": False, "reason": "키 없음"}
    now = datetime.now(KST)
    nx, ny = to_grid(lat, lon)
    bd, bt = base_time()
    skey = cache.make_key("wx_short", nx=nx, ny=ny, bd=bd, bt=bt)
    short = cache.get(conn, "weather", skey)
    if not short:
        items = _call(conn, URL, "getVilageFcst", {"numOfRows": 1500, "base_date": bd, "base_time": bt,
                                                   "nx": nx, "ny": ny})
        if items:
            days, hours = _short_days(items, now)
            short = {"days": days, "hours": hours}
            cache.put(conn, "weather", skey, short)
    mid = _mid(conn, lat, lon)
    # 겹치는 날은 동네 단위 단기예보가 우선. 단, 단기예보가 그날 일부(새벽 몇 시간)만 있으면
    # 비어 있는 칸(오전·오후·기온)은 중기예보로 채운다
    md, sd = (mid or {}).get("days") or {}, (short or {}).get("days") or {}
    merged, from_short = {}, set()
    for date in set(md) | set(sd):
        s_, m_ = sd.get(date) or {}, md.get(date) or {}
        row = {k: (s_.get(k) if s_.get(k) is not None else m_.get(k)) for k in ("am", "pm", "tmin", "tmax")}
        if s_.get("am") and s_.get("pm"):
            from_short.add(date)
        merged[date] = row
    today = now.strftime("%Y%m%d")
    out_days = []
    for date in sorted(merged):
        if date < today:
            continue
        dt = datetime.strptime(date, "%Y%m%d")
        out_days.append({"date": dt.strftime("%Y-%m-%d"), "dow": DOW_KO[dt.weekday()],
                         "src": "short" if date in from_short else "mid", **merged[date]})
    # 지금·시간별은 읽을 때 자른다 (캐시가 한 시간 묵어도 '지금' 은 맞게)
    nowkey = now.strftime("%Y%m%d%H00")
    hours = [h for h in (short or {}).get("hours", []) if h["t"] >= nowkey][:24]
    for h in hours:
        h["h"] = int(h["t"][8:10])
    return {"ok": bool(out_days), "now": hours[0] if hours else None, "hours": hours, "days": out_days[:11],
            "mid_ok": bool(mid), "region": (mid or {}).get("land"), "city": (mid or {}).get("city"),
            "base": f"{bd} {bt}"}
