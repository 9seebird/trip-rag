"""비즈라우터 클라이언트 — 임베딩 / 리랭킹 / 생성.

임베딩과 생성은 OpenAI 호환이라 형식이 같고, 리랭킹만 표준이 아니라 직접 호출한다.
응답의 usage.cost 에 원화 비용이 오므로 매 호출을 api_calls 에 남긴다.
이게 나중에 "인덱싱 총 얼마 / 질문당 얼마" 표의 근거가 된다.
"""
import time
import httpx

from .config import (BIZROUTER_API_KEY, BIZROUTER_BASE, EMBED_MODEL, EMBED_DIM,
                     RERANK_MODEL, CHAT_MODEL)
from .db import log_call


def _headers():
    if not BIZROUTER_API_KEY:
        raise RuntimeError(".env 의 BIZROUTER_API_KEY 가 비어 있습니다.")
    return {"Authorization": f"Bearer {BIZROUTER_API_KEY}",
            "Content-Type": "application/json"}


def _post(conn, path, payload, op, timeout=120.0, retries=3):
    url = f"{BIZROUTER_BASE}{path}"
    last = None
    for attempt in range(retries):
        t0 = time.time()
        try:
            r = httpx.post(url, json=payload, headers=_headers(), timeout=timeout)
            ms = int((time.time() - t0) * 1000)
            if r.status_code >= 400:
                log_call(conn, "bizrouter", op, False, r.status_code, ms, note=r.text[:300])
                if r.status_code in (429, 500, 502, 503, 504):
                    time.sleep(2 * (attempt + 1))
                    last = RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
                    continue
                raise RuntimeError(f"HTTP {r.status_code}: {r.text[:300]}")
            data = r.json()
            usage = data.get("usage") or {}
            log_call(conn, "bizrouter", op, True, r.status_code, ms,
                     tokens=usage.get("total_tokens") or usage.get("prompt_tokens"),
                     cost=usage.get("cost"), note=payload.get("model"))
            return data
        except (httpx.TimeoutException, httpx.TransportError) as e:
            last = e
            log_call(conn, "bizrouter", op, False, None, int((time.time() - t0) * 1000),
                     note=str(e)[:200])
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"비즈라우터 호출 실패: {last}")


def embed(conn, texts, model=None, dim=None):
    """텍스트 리스트 → 벡터 리스트. 순서는 입력과 같게 맞춰 돌려준다."""
    model = model or EMBED_MODEL
    dim = dim or EMBED_DIM
    data = _post(conn, "/embeddings", {
        "model": model, "input": texts, "dimensions": dim,
    }, "embeddings")
    rows = sorted(data["data"], key=lambda x: x.get("index", 0))
    vecs = [r["embedding"] for r in rows]
    if len(vecs) != len(texts):
        raise RuntimeError(f"임베딩 개수 불일치: 요청 {len(texts)} 응답 {len(vecs)}")
    got = len(vecs[0])
    if got != dim:
        raise RuntimeError(f"차원 불일치: 요청 {dim} 응답 {got}. "
                           "pgvector HNSW 는 2000차원까지만 인덱싱됩니다.")
    return vecs


def rerank(conn, query, documents, top_n=5, model=None):
    data = _post(conn, "/rerank", {
        "model": model or RERANK_MODEL, "query": query,
        "documents": documents, "top_n": top_n,
    }, "rerank")
    return data.get("results") or data.get("data") or []


def chat(conn, messages, model=None, **kw):
    data = _post(conn, "/chat/completions", {
        "model": model or CHAT_MODEL, "messages": messages, **kw,
    }, "chat")
    return data["choices"][0]["message"]["content"]


def to_pgvector(v):
    """psycopg 로 vector 컬럼에 넣을 문자열."""
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"
