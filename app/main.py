import hmac
import os
import time

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from ingest.config import EMBED_MODEL
from ingest.db import connect
from ingest.migrate import apply as apply_migrations
from . import cache, limits, weather
from .course import build as build_course
from .extract import AREA_NAME
from .search import find as run_find, run as run_search

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
):
    """서비스 화면용 검색 (조건 필터 + 의미 검색). 같은 질문은 캐시에서 바로 돌려준다."""
    near = (lat, lon, radius) if lat is not None and lon is not None else None
    qn = " ".join(q.split())
    # 위치는 약 100m 단위로 묶어 캐시를 같이 쓴다. 날짜를 넣는 건 '이번 주말 축제' 같은 질문 때문
    key = cache.make_key("find", q=qn, area=area, limit=limit, day=time.strftime("%Y%m%d"),
                         near=[round(lat, 3), round(lon, 3), radius] if near else None)
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
            out = run_find(conn, qn, limit, overrides={"area": area}, use_rerank=False, near=near, hybrid=True)
        except Exception as e:
            cache.log(conn, "find", qn, area, bool(near), None, int((time.time() - t0) * 1000),
                      ok=False, note=str(e))
            raise
        cache.put(conn, "find", key, out)
        cache.log(conn, "find", qn, area, bool(near), len(out["items"]), int((time.time() - t0) * 1000))
        return out


@app.get("/api/weather", dependencies=limited)
def get_weather(lat: float = Query(..., ge=33, le=39), lon: float = Query(..., ge=124, le=132)):
    """앞으로 12시간 날씨 요약 (기상청 단기예보). 활용신청 전이면 ok=False."""
    with connect() as conn:
        return weather.forecast(conn, lat, lon)


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
    for a in areas:
        a["name"] = AREA_NAME.get(a["area_code"], str(a["area_code"]))
    return {"days": days, "daily": daily, "top": top, "zero": zero, "recent": recent,
            "models_today": models, "tour_calls_today": tour, "areas": areas,
            "festivals": festivals, "extras": extras, "cache": cache_rows, "requests": limits.usage()}


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
