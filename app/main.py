import hmac
import os
import time

import re

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ingest.config import EMBED_MODEL
from ingest.db import connect
from ingest.migrate import apply as apply_migrations
from . import cache, limits, weather
from .course import build as build_course
from .extract import AREA_NAME
from .search import attach_crowd, crowd_level, find as run_find, run as run_search

# 운영(PUBLIC=1)에서는 API 문서·통계를 닫는다. 통계에는 비용이 찍힌다.
PUBLIC = os.getenv("PUBLIC", "0") == "1"
KAKAO_JS_KEY = os.getenv("KAKAO_JS_KEY", "")
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")          # 비어 있으면 관리 API 를 닫는다

app = FastAPI(title="trip-rag", docs_url=None if PUBLIC else "/docs",
              redoc_url=None, openapi_url=None if PUBLIC else "/openapi.json")
limited = [Depends(limits.check)]


@app.on_event("startup")
def _migrate():
    """init.sql 은 DB 첫 기동 때만 돈다. 그 뒤의 스키마 변경은 여기서 따라잡는다."""
    try:
        with connect() as conn:
            applied = apply_migrations(conn, quiet=True)
        if applied:
            print(f"[migrate] 적용: {', '.join(applied)}")
    except Exception as e:
        print(f"[migrate] 실패(무시하고 기동): {e}")


@app.get("/api/health")
def health():
    with connect() as conn:
        conn.execute("SELECT 1")
    return {"ok": True}


@app.get("/api/config")
def config():
    """화면이 처음 한 번 부른다. 지역 목록은 '검색 가능한(색인된) 곳이 있는 지역' 만."""
    with connect() as conn:
        rows = conn.execute(
            """SELECT p.area_code, count(DISTINCT p.content_id) places,
                      count(DISTINCT c.content_id) indexed
               FROM places p LEFT JOIN chunks c ON c.content_id = p.content_id AND c.model = %s
               GROUP BY p.area_code ORDER BY p.area_code""", (EMBED_MODEL,)).fetchall()
    regions = [{"code": r["area_code"], "name": AREA_NAME.get(r["area_code"], str(r["area_code"])),
                "places": r["places"], "indexed": r["indexed"]}
               for r in rows if r["indexed"] > 0]
    return {"kakao_js_key": KAKAO_JS_KEY, "regions": regions}


@app.get("/api/stats")
def stats():
    if PUBLIC:
        raise HTTPException(404)
    with connect() as conn:
        progress = [dict(r) for r in conn.execute("SELECT * FROM ingest_progress").fetchall()]
        used = conn.execute("SELECT used FROM tour_quota_today").fetchone()["used"]
        models = [dict(r) for r in conn.execute(
            """SELECT model, dim, count(*) chunks, count(DISTINCT content_id) places
               FROM chunks GROUP BY model, dim""").fetchall()]
        cost = conn.execute(
            """SELECT coalesce(sum(cost_krw),0) krw, coalesce(sum(tokens),0) tok
               FROM api_calls WHERE api='bizrouter'""").fetchone()
    return {"tour_calls_today": used, "progress": progress, "models": models,
            "bizrouter_cost_krw": float(cost["krw"]), "bizrouter_tokens": cost["tok"],
            "requests": limits.usage()}


@app.get("/api/find", dependencies=limited)
def find(
    q: str = Query(..., min_length=1, max_length=100),
    area: int | None = None,
    limit: int = Query(12, ge=1, le=20),
    lat: float | None = Query(None, ge=33, le=39, description="'내 근처' — 위도 (국내 범위)"),
    lon: float | None = Query(None, ge=124, le=132),
    radius: float = Query(10, ge=1, le=50, description="'내 근처' 반경 km"),
    off: str | None = Query(None, max_length=60, description="질문에서 뽑았지만 끈 조건. 쉼표로 (area,indoor,pet,access,types,when)"),
    indoor: bool | None = Query(None, description="조건 칩으로 직접 켠 실내(true)/야외(false)"),
    pet: bool | None = None,
    access: bool | None = None,
):
    """서비스 화면용 검색 (조건 필터 + 의미 검색). 같은 질문은 캐시에서 바로 돌려준다."""
    near = (lat, lon, radius) if lat is not None and lon is not None else None
    qn = " ".join(q.split())
    offs = sorted({f for f in (off or "").split(",") if f in OFFABLE})
    adds = {k: v for k, v in (("indoor", indoor), ("pet", pet or None), ("access", access or None)) if v is not None}
    # 위치는 약 100m 단위로 묶어 캐시를 같이 쓴다. 날짜를 넣는 건 '이번 주말 축제' 같은 질문 때문
    key = cache.make_key("find", q=qn, area=area, limit=limit, day=time.strftime("%Y%m%d"),
                         near=[round(lat, 3), round(lon, 3), radius] if near else None,
                         off=offs or None, add=adds or None)
    t0 = time.time()
    with connect() as conn:
        hit = cache.get(conn, "find", key)
        if hit:
            cache.log(conn, "find", qn, area, bool(near), len(hit.get("items", [])),
                      int((time.time() - t0) * 1000), cached=True)
            return hit | {"cached": True}
        try:
            # Gemini 생성 모델 리랭크는 20~40초라 5초 상한에 거의 항상 걸린다 → 리랭커 대신 하이브리드(키워드+의미).
            # 빠른 리랭커가 생기면 use_rerank=True 로 되돌린다 (상한은 search.RERANK_TIMEOUT).
            out = run_find(conn, qn, limit, overrides={"area": area, **adds}, use_rerank=False, near=near,
                           hybrid=True, off=offs)
        except Exception as e:
            cache.log(conn, "find", qn, area, bool(near), None, int((time.time() - t0) * 1000),
                      ok=False, note=str(e))
            raise
        cache.put(conn, "find", key, out)
        cache.log(conn, "find", qn, area, bool(near), len(out["items"]), int((time.time() - t0) * 1000))
        return out


OFFABLE = {"area", "indoor", "pet", "access", "types", "when", "baby"}


# ── 결과 피드백 ──────────────────────────────────────────────────────────────
class Feedback(BaseModel):
    q: str = Field(..., min_length=1, max_length=100)
    vote: int = Field(..., ge=-1, le=1)
    content_id: int | None = None          # 없으면 '이 검색 결과 전체'
    rank: int | None = Field(None, ge=1, le=50)
    area: int | None = None


@app.post("/api/feedback", dependencies=limited)
def feedback(f: Feedback):
    """좋아요(1)/별로(-1). 0 이면 같은 (질문, 장소)의 이전 표시를 지운다(취소)."""
    q = " ".join(f.q.split())
    with connect() as conn:
        # 한 사람이 같은 걸 여러 번 누르는 걸 구별할 수 없어(IP 를 안 남긴다) 화면이 한 번만 보내게 한다.
        # 취소는 가장 최근 것 하나만 지운다
        if f.vote == 0:
            conn.execute("""DELETE FROM feedback WHERE id = (
                              SELECT id FROM feedback WHERE q = %s AND content_id IS NOT DISTINCT FROM %s
                              ORDER BY ts DESC LIMIT 1)""", (q, f.content_id))
            return {"ok": True}
        if f.content_id is not None and not conn.execute(
                "SELECT 1 FROM places WHERE content_id = %s", (f.content_id,)).fetchone():
            raise HTTPException(404, "장소가 없습니다")
        conn.execute("INSERT INTO feedback (q, area, content_id, rank, vote) VALUES (%s, %s, %s, %s, %s)",
                     (q, f.area, f.content_id, f.rank, f.vote))
    return {"ok": True}


# ── 홈 화면: 많이 찾는 검색어 · 이번 주 축제 · 주말에 여유로운 곳 ───────────────────────
_POPULAR = {"t": 0, "v": []}
_SAFE_Q = re.compile(r"^[0-9A-Za-z가-힣\s·,./~!?&()+'-]{2,30}$")


def popular_queries(conn, n=8):
    """최근 7일 검색어 중 결과가 있었고 3번 이상 나온 것. 10분마다 다시 센다.
    누가 찾았는지는 남기지 않으니 '여러 번 나온 말' 만 보여 준다 (개인정보·장난 입력이 한 번으로 뜨지 않게)."""
    if time.time() - _POPULAR["t"] < 600:
        return _POPULAR["v"]
    rows = conn.execute("""SELECT q, count(*) n FROM search_log
                           WHERE kind = 'find' AND ok AND results > 0 AND ts > now() - interval '7 days'
                           GROUP BY q HAVING count(*) >= 3 ORDER BY n DESC, max(ts) DESC LIMIT 40""").fetchall()
    out = [r["q"] for r in rows if _SAFE_Q.match(r["q"]) and not re.search(r"\d{4,}", r["q"])][:n]
    _POPULAR.update(t=time.time(), v=out)
    return out


@app.get("/api/home", dependencies=limited)
def home(area: int | None = None, lat: float | None = Query(None, ge=33, le=39),
         lon: float | None = Query(None, ge=124, le=132)):
    """검색 전 첫 화면. 지역을 고르면 그 지역, '내 근처' 면 반경 30km."""
    key = cache.make_key("home", area=area, day=time.strftime("%Y%m%d%H"),
                         near=[round(lat, 2), round(lon, 2)] if lat is not None and lon is not None else None)
    with connect() as conn:
        hit = cache.get(conn, "home", key)
        if hit:
            return hit | {"popular": popular_queries(conn)}
        where, params = ["TRUE"], {}
        if lat is not None and lon is not None:
            where.append("abs(p.lat - %(lat)s) < 0.27 AND abs(p.lon - %(lon)s) < 0.33")
            params.update(lat=lat, lon=lon)
        elif area:
            where.append("p.area_code = %(area)s"); params["area"] = area
        w = " AND ".join(where)
        # 오늘~7일 안에 하는 축제. 사진 있는 것·곧 시작하는 것 먼저
        fest = conn.execute(f"""
            SELECT p.content_id, p.title, p.addr, p.image_url image, p.area_code, p.content_type, p.lat, p.lon,
                   to_char(p.event_start, 'YYYY-MM-DD') event_start, to_char(p.event_end, 'YYYY-MM-DD') event_end
            FROM places p
            WHERE {w} AND p.content_type = 15 AND p.event_start IS NOT NULL
              AND p.event_start <= (now() AT TIME ZONE 'Asia/Seoul')::date + 7
              AND p.event_end >= (now() AT TIME ZONE 'Asia/Seoul')::date
            ORDER BY (p.image_url IS NULL), p.event_start, p.content_id LIMIT 12""", params).fetchall()
        # 이번 주말(다가오는 토·일) 예측이 낮은 관광지. 사진 있는 곳만 (첫 화면이라)
        calm = []
        try:
            calm = conn.execute(f"""
                WITH wk AS (
                  SELECT c.content_id, avg(c.rate) rate FROM crowd_forecast c
                  WHERE c.content_id IS NOT NULL AND extract(isodow FROM c.ymd) IN (6, 7)
                    AND c.ymd BETWEEN (now() AT TIME ZONE 'Asia/Seoul')::date
                                  AND (now() AT TIME ZONE 'Asia/Seoul')::date + 7
                  GROUP BY c.content_id)
                SELECT p.content_id, p.title, p.addr, p.image_url image, p.area_code, p.content_type,
                       p.lat, p.lon, round(wk.rate::numeric, 1)::float rate
                FROM wk JOIN places p ON p.content_id = wk.content_id
                WHERE {w} AND p.image_url IS NOT NULL AND wk.rate < 50
                ORDER BY wk.rate, p.content_id LIMIT 12""", params).fetchall()
        except Exception:
            calm = []
        out = {"festivals": [dict(r) for r in fest],
               "calm": [dict(r) | {"level": crowd_level(r["rate"])} for r in calm]}
        cache.put(conn, "home", key, out)
        return out | {"popular": popular_queries(conn)}


@app.get("/api/weather", dependencies=limited)
def get_weather(lat: float = Query(..., ge=33, le=39), lon: float = Query(..., ge=124, le=132)):
    """앞으로 12시간 날씨 요약 (기상청 단기예보). 활용신청 전이면 ok=False."""
    with connect() as conn:
        return weather.forecast(conn, lat, lon)


@app.get("/api/weather/week", dependencies=limited)
def get_weather_week(lat: float = Query(..., ge=33, le=39), lon: float = Query(..., ge=124, le=132)):
    """날씨 창: 지금 + 오늘부터 10일 (단기예보 + 중기예보). 중기예보 미신청이면 3~4일치만."""
    with connect() as conn:
        return weather.week(conn, round(lat, 2), round(lon, 2))


@app.get("/api/search", dependencies=limited)
def search(
    q: str = Query(..., min_length=1, max_length=100),
    limit: int = Query(8, ge=1, le=20),
    compare: str = Query("keyword", pattern="^(keyword|rerank|hybrid)$"),
    filters: bool = True,
    pool: int = 30,
    area: int | None = None,
    indoor: bool | None = None,
    pet: bool | None = None,
    baby: bool | None = None,
    types: str | None = Query(None, description="쉼표로. 예: 12,14"),
):
    ov = {"area": area, "indoor": indoor, "pet": pet, "baby": baby,
          "types": [int(t) for t in types.split(",")] if types else None}
    with connect() as conn:
        return run_search(conn, q, limit, compare, filters, pool, ov)


@app.get("/api/course", dependencies=limited)
def course(
    id: int = Query(..., description="기준 장소 content_id"),
    radius: float = Query(5.0, ge=1, le=30, description="후보 반경 km. 후보가 없으면 칸마다 2배·4배로 넓힌다"),
    llm: bool = Query(True, description="false 면 모델 없이 규칙(가장 가까운 곳 + 최단 순서)"),
):
    key = cache.make_key("course", id=id, radius=radius, llm=llm)
    t0 = time.time()
    with connect() as conn:
        hit = cache.get(conn, "course", key)
        if hit:
            cache.log(conn, "course", str(id), None, results=len(hit.get("stops", [])),
                      took_ms=int((time.time() - t0) * 1000), cached=True)
            return hit | {"cached": True}
        out = build_course(conn, id, radius, llm)
        ok = "error" not in out
        if ok:                                      # 혼잡도 예측이 있는 곳은 붙여 둔다 (코스 날짜 값은 화면이 고른다)
            attach_crowd(conn, [out["start"], *out["stops"], *[c for cs in out["candidates"].values() for c in cs]])
        if ok and out.get("source") == "llm":          # 모델이 실패해 규칙으로 짠 코스는 다음에 다시 시도
            cache.put(conn, "course", key, out)
        cache.log(conn, "course", str(id), None, results=len(out.get("stops", [])),
                  took_ms=int((time.time() - t0) * 1000), ok=ok,
                  note=out.get("error") or ("규칙" if out.get("source") == "rule" else None))
        return out


# ── 관리 화면 (/admin) ────────────────────────────────────────────────────────
def _admin(token: str | None):
    if not ADMIN_TOKEN:
        raise HTTPException(404)
    if not token or not hmac.compare_digest(token, ADMIN_TOKEN):
        raise HTTPException(401, "관리 토큰이 맞지 않습니다")


@app.get("/api/admin/stats")
def admin_stats(days: int = Query(7, ge=1, le=60), x_admin_token: str | None = Header(None)):
    """검색 기록·캐시·모델 호출·수집 현황. 헤더 X-Admin-Token 이 .env 의 ADMIN_TOKEN 과 같아야 한다."""
    _admin(x_admin_token)
    iv = f"{days} days"
    with connect() as conn:
        q = lambda sql, *a: [dict(r) for r in conn.execute(sql, a).fetchall()]
        daily = q("""SELECT to_char(ts AT TIME ZONE 'Asia/Seoul', 'MM-DD') d, kind,
                            count(*) n, count(*) FILTER (WHERE cached) cached,
                            count(*) FILTER (WHERE NOT ok) failed,
                            round(avg(took_ms) FILTER (WHERE NOT cached)) avg_ms,
                            percentile_disc(0.9) WITHIN GROUP (ORDER BY took_ms)
                              FILTER (WHERE NOT cached) p90_ms
                     FROM search_log WHERE ts > now() - %s::interval
                     GROUP BY 1, 2 ORDER BY 1, 2""", iv)
        top = q("""SELECT q, count(*) n, max(results) results FROM search_log
                   WHERE kind = 'find' AND ts > now() - %s::interval
                   GROUP BY q ORDER BY n DESC, q LIMIT 20""", iv)
        zero = q("""SELECT q, count(*) n, max(ts) last FROM search_log
                    WHERE kind = 'find' AND ok AND results = 0 AND ts > now() - %s::interval
                    GROUP BY q ORDER BY n DESC, last DESC LIMIT 20""", iv)
        recent = q("""SELECT l.ts, l.kind, coalesce(p.title, l.q) q, l.area, l.near, l.results, l.took_ms,
                             l.cached, l.ok, l.note
                      FROM search_log l
                      LEFT JOIN places p ON l.kind = 'course' AND p.content_id::text = l.q
                      ORDER BY l.ts DESC LIMIT 40""")
        models = q("""SELECT api, op, count(*) n, count(*) FILTER (WHERE NOT ok) failed,
                             round(avg(ms)) avg_ms
                      FROM api_calls
                      WHERE ts > date_trunc('day', now() AT TIME ZONE 'Asia/Seoul') AT TIME ZONE 'Asia/Seoul'
                      GROUP BY api, op ORDER BY api, op""")
        areas = q("""SELECT p.area_code, count(*) places,
                            count(*) FILTER (WHERE p.detail_status = 'done') detail_done,
                            count(DISTINCT c.content_id) indexed
                     FROM places p LEFT JOIN chunks c ON c.content_id = p.content_id AND c.model = %s
                     GROUP BY p.area_code ORDER BY p.area_code""", EMBED_MODEL)
        festivals = q("""SELECT count(*) FILTER (WHERE event_end >= current_date) upcoming,
                                count(*) FILTER (WHERE event_end IS NOT NULL) dated
                         FROM places WHERE content_type = 15""")[0]
        cache_rows = q("SELECT kind, count(*) n FROM api_cache GROUP BY kind ORDER BY kind")
        extras = q("""SELECT count(*) FILTER (WHERE pet_allowed) pet,
                             count(*) FILTER (WHERE access_listed) access,
                             count(*) FILTER (WHERE image_url IS NULL) no_image
                      FROM places""")[0]
        tour = conn.execute("SELECT used FROM tour_quota_today").fetchone()["used"]
        fb_sum = q("""SELECT count(*) FILTER (WHERE vote = 1) up, count(*) FILTER (WHERE vote = -1) down,
                             count(*) FILTER (WHERE content_id IS NULL AND vote = -1) search_down
                      FROM feedback WHERE ts > now() - %s::interval""", iv)[0]
        fb_recent = q("""SELECT f.ts, f.q, f.vote, f.rank, p.title
                         FROM feedback f LEFT JOIN places p ON p.content_id = f.content_id
                         ORDER BY f.ts DESC LIMIT 30""")
        # 별로가 많은 검색어 — 검색 품질을 먼저 손볼 곳
        fb_bad = q("""SELECT q, count(*) FILTER (WHERE vote = -1) down, count(*) FILTER (WHERE vote = 1) up
                      FROM feedback WHERE ts > now() - %s::interval
                      GROUP BY q HAVING count(*) FILTER (WHERE vote = -1) > 0
                      ORDER BY down DESC, up LIMIT 15""", iv)
        crowd = q("""SELECT count(DISTINCT content_id) linked, count(DISTINCT name) names,
                            (SELECT count(*) FROM crowd_lists) sgg,
                            (SELECT count(*) FROM places WHERE ldong_sgg IS NOT NULL) coded
                     FROM crowd_forecast""")[0]
    for a in areas:
        a["name"] = AREA_NAME.get(a["area_code"], str(a["area_code"]))
    return {"days": days, "daily": daily, "top": top, "zero": zero, "recent": recent,
            "models_today": models, "tour_calls_today": tour, "areas": areas,
            "festivals": festivals, "extras": extras, "cache": cache_rows, "requests": limits.usage(),
            "feedback": {"summary": fb_sum, "recent": fb_recent, "bad": fb_bad}, "crowd": crowd}


@app.get("/admin")
def admin_page():
    if not ADMIN_TOKEN:
        raise HTTPException(404)
    return FileResponse("static/admin.html")


@app.get("/manifest.webmanifest")
def manifest():
    """홈 화면에 추가(PWA). 확장자를 모르는 서버가 text/plain 으로 주면 일부 브라우저가 무시해서 직접 준다."""
    return FileResponse("static/manifest.webmanifest", media_type="application/manifest+json")


@app.get("/lab")
def lab():
    """검색 방식 비교 화면 (포트폴리오 설명용)."""
    return FileResponse("static/lab.html")


app.mount("/", StaticFiles(directory="static", html=True), name="static")
