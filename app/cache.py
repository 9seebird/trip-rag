"""응답 캐시와 검색 기록.

캐시 — 같은 질문을 또 받으면 임베딩(Gemini)을 부르지 않고 저장해 둔 결과를 바로 돌려준다.
  예시 질문 칩은 누구나 누르므로 효과가 크고, 무료 한도(하루·분당)를 아낀다.
  매일 새 장소가 들어오므로 오래 두지 않는다 (find 12시간, course 24시간, weather 1시간).

기록 — 관리 화면(/admin)에서 '자주 묻는 질문', '결과 0건 질문', 응답 시간을 보려고 남긴다.
  IP·좌표는 남기지 않는다. 기록이 실패해도 검색은 돼야 하므로 예외는 삼킨다.
"""
import hashlib
import json
import random

TTL_HOURS = {"find": 12, "course": 24, "weather": 1}
KEEP_DAYS = 7                     # 이보다 오래된 캐시는 가끔 한 번씩 지운다


def make_key(kind, **params):
    raw = json.dumps(params, ensure_ascii=False, sort_keys=True, default=str)
    return f"{kind}:" + hashlib.sha1(raw.encode("utf-8")).hexdigest()


def get(conn, kind, key):
    try:
        row = conn.execute(
            "SELECT value FROM api_cache WHERE key = %s AND created_at > now() - make_interval(hours => %s)",
            (key, TTL_HOURS.get(kind, 1))).fetchone()
        return row["value"] if row else None
    except Exception:
        return None


def put(conn, kind, key, value):
    try:
        conn.execute(
            """INSERT INTO api_cache (key, kind, value) VALUES (%s, %s, %s::jsonb)
               ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, created_at = now()""",
            (key, kind, json.dumps(value, ensure_ascii=False, default=str)))
        if random.random() < 0.02:
            conn.execute("DELETE FROM api_cache WHERE created_at < now() - make_interval(days => %s)",
                         (KEEP_DAYS,))
    except Exception:
        pass


def log(conn, kind, q=None, area=None, near=False, results=None, took_ms=None,
        cached=False, ok=True, note=None):
    try:
        conn.execute(
            """INSERT INTO search_log (kind, q, area, near, results, took_ms, cached, ok, note)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (kind, (q or "")[:200], area, near, results, took_ms, cached, ok, (note or "")[:300] or None))
    except Exception:
        pass
