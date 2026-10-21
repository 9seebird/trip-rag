import os

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from ingest.config import EMBED_MODEL
from ingest.db import connect
from ingest.migrate import apply as apply_migrations
from . import limits
from .course import build as build_course
from .extract import AREA_NAME
from .search import find as run_find, run as run_search

# 운영(PUBLIC=1)에서는 API 문서·통계를 닫는다. 통계에는 비용이 찍힌다.
PUBLIC = os.getenv("PUBLIC", "0") == "1"
KAKAO_JS_KEY = os.getenv("KAKAO_JS_KEY", "")

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
):
    """서비스 화면용 검색 (조건 필터 + 의미 검색 + 리랭커)."""
    with connect() as conn:
        # Gemini 생성 모델 리랭크는 20~40초라 5초 상한에 거의 항상 걸린다 → 기다리지 않고 벡터 순서로.
        # 빠른 리랭커가 생기면 use_rerank=True 로 되돌린다 (상한은 search.RERANK_TIMEOUT).
        return run_find(conn, q, limit, overrides={"area": area}, use_rerank=False)


@app.get("/api/search", dependencies=limited)
def search(
    q: str = Query(..., min_length=1, max_length=100),
    limit: int = Query(8, ge=1, le=20),
    compare: str = Query("keyword", pattern="^(keyword|rerank)$"),
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
    with connect() as conn:
        return build_course(conn, id, radius, llm)


@app.get("/lab")
def lab():
    """검색 방식 비교 화면 (포트폴리오 설명용)."""
    return FileResponse("static/lab.html")


app.mount("/", StaticFiles(directory="static", html=True), name="static")
