"""카카오 로컬 '키워드로 장소 검색' — 반나절 데이터(관광공사)에 없는 가게를 보충한다.

언제 부르나: 질문의 말이 데이터에 없거나(가맥집·전일갑오) 줄임말을 넓혀 찾았을 때만 (search.find 의 weak·unknown·expanded).
           평범한 질문은 부르지 않는다 — 우리 데이터가 먼저고, 무료 쿼터를 아낀다.
무엇을 하나: 이름·분류·주소·카카오맵 링크만 화면에 보여 준다. DB 에는 저장하지 않는다(카카오 이용 정책).
           그래서 이 장소들로는 반나절 코스를 짜지 않는다.

키: developers.kakao.com → 내 애플리케이션 → 앱 키 → REST API 키 를 KAKAO_REST_KEY 에.
    비워 두면 이 기능 전체가 꺼진다. 앱의 [카카오맵] 사용 설정이 ON 이어야 한다.
"""
import os
import time

import httpx

from ingest.db import log_call, used_today

URL = "https://dapi.kakao.com/v2/local/search/keyword.json"
KEY = os.getenv("KAKAO_REST_KEY", "").strip()
DAILY_LIMIT = int(os.getenv("KAKAO_DAILY_LIMIT", "3000"))   # 무료 쿼터 안에서 넉넉히. 넘으면 그날은 안 부른다
TIMEOUT = 3.0                                               # 검색 화면을 오래 붙잡지 않는다


def enabled():
    return bool(KEY)


def keyword(conn, query, near=None, size=8):
    """[{name, category, addr, phone, url, lat, lon}] — 실패하면 빈 목록 (검색 화면은 이것 없이도 돈다).

    near = (위도, 경도, 반경km) 이면 그 주변만, 가까운 순.
    """
    query = " ".join((query or "").split())
    if not KEY or not query:
        return []
    if used_today(conn, "kakao") >= DAILY_LIMIT:
        return []
    params = {"query": query[:100], "size": max(1, min(size, 15))}
    if near:
        lat, lon, km = near
        params.update(y=lat, x=lon, radius=int(min(km, 20) * 1000), sort="distance")
    t0 = time.time()
    try:
        r = httpx.get(URL, params=params, headers={"Authorization": f"KakaoAK {KEY}"}, timeout=TIMEOUT)
        ms = int((time.time() - t0) * 1000)
        log_call(conn, "kakao", "keyword", r.status_code == 200, r.status_code, ms,
                 note=None if r.status_code == 200 else r.text[:200])
        if r.status_code != 200:
            return []
        docs = r.json().get("documents") or []
    except (httpx.HTTPError, ValueError) as e:
        log_call(conn, "kakao", "keyword", False, None, int((time.time() - t0) * 1000), note=str(e)[:200])
        return []
    out = []
    for d in docs:
        try:
            lat, lon = float(d["y"]), float(d["x"])
        except (KeyError, TypeError, ValueError):
            lat = lon = None
        out.append({
            "name": d.get("place_name") or "",
            "category": (d.get("category_name") or "").split(" > ")[-1],
            "addr": d.get("road_address_name") or d.get("address_name") or "",
            "phone": d.get("phone") or "",
            "url": d.get("place_url") or "",
            "lat": lat, "lon": lon,
        })
    return [o for o in out if o["name"]]
