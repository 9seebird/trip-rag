from fastapi import FastAPI, Query
from fastapi.staticfiles import StaticFiles

from ingest.db import connect
from ingest.migrate import apply as apply_migrations
from .search import run as run_search

app = FastAPI(title="trip-rag")


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


@app.get("/api/stats")
def stats():
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
            "bizrouter_cost_krw": float(cost["krw"]), "bizrouter_tokens": cost["tok"]}


@app.get("/api/search")
def search(
    q: str = Query(..., min_length=1),
    limit: int = 8,
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


app.mount("/", StaticFiles(directory="static", html=True), name="static")
