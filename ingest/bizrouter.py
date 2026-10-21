"""모델 클라이언트 — 임베딩 / 리랭킹 / 생성.

제공처는 .env 의 LLM_PROVIDER 로 고른다.
  bizrouter : 임베딩·생성은 OpenAI 호환, 리랭킹은 전용 /rerank. 응답 usage.cost 에 원화 비용이 온다.
  gemini    : 구글 AI Studio 키 하나로.
              임베딩은 Gemini 고유 API(batchEmbedContents) — 차원을 1024 로 줄이고(outputDimensionality),
              문서/질문을 구분해(taskType) 넣을 수 있어서 OpenAI 호환 대신 이쪽을 쓴다.
              생성은 OpenAI 호환 엔드포인트(/openai/chat/completions).
              리랭커 API 가 없어서, 생성 모델이 후보를 읽고 순서를 매긴다.

모든 호출을 api_calls 에 남긴다 (api 이름 = 제공처). 비용은 bizrouter 만 응답에 온다.
"""
import json
import re
import time
import httpx

from .config import (LLM_PROVIDER, BIZROUTER_API_KEY, BIZROUTER_BASE, GEMINI_API_KEY, GEMINI_BASE,
                     EMBED_MODEL, EMBED_DIM, RERANK_MODEL, CHAT_MODEL)
from .db import log_call


def _key():
    key = GEMINI_API_KEY if LLM_PROVIDER == "gemini" else BIZROUTER_API_KEY
    if not key:
        name = "GEMINI_API_KEY" if LLM_PROVIDER == "gemini" else "BIZROUTER_API_KEY"
        raise RuntimeError(f".env 의 {name} 가 비어 있습니다.")
    return key


def _wait_429(text, attempt):
    """한도 초과(429)면 얼마나 쉴지. Gemini 는 응답에 retryDelay("37s")를 준다."""
    m = re.search(r'"retryDelay"\s*:\s*"(\d+(?:\.\d+)?)s"', text or "")
    return float(m.group(1)) + 1 if m else min(60, 10 * (attempt + 1))


def _post(conn, url, payload, op, headers, timeout=120.0, retries=3):
    last = None
    # 429 는 분당 한도라 기다리면 풀린다. 무료 등급에서 색인을 끝까지 돌릴 수 있게 넉넉히 기다린다
    for attempt in range(max(retries, 10)):
        t0 = time.time()
        try:
            r = httpx.post(url, json=payload, headers=headers, timeout=timeout)
            ms = int((time.time() - t0) * 1000)
            if r.status_code >= 400:
                log_call(conn, LLM_PROVIDER, op, False, r.status_code, ms, note=r.text[:300])
                last = RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
                if r.status_code == 429:
                    wait = _wait_429(r.text, attempt)
                    if wait > 90:                       # 하루 한도 같은 긴 대기는 기다리지 않는다
                        raise RuntimeError(f"HTTP 429 (한도 소진, {wait:.0f}초 뒤 풀림): {r.text[:200]}")
                    print(f"  한도 초과(429) — {wait:.0f}초 쉬고 다시", flush=True)
                    time.sleep(wait)
                    continue
                if r.status_code in (500, 502, 503, 504) and attempt < retries - 1:
                    time.sleep(2 * (attempt + 1))
                    continue
                raise RuntimeError(f"HTTP {r.status_code}: {r.text[:300]}")
            data = r.json()
            usage = data.get("usage") or data.get("usageMetadata") or {}
            log_call(conn, LLM_PROVIDER, op, True, r.status_code, ms,
                     tokens=usage.get("total_tokens") or usage.get("prompt_tokens")
                     or usage.get("totalTokenCount"),
                     cost=usage.get("cost"), note=payload.get("model"))
            return data
        except (httpx.TimeoutException, httpx.TransportError) as e:
            last = e
            if attempt >= retries - 1:
                break
            log_call(conn, LLM_PROVIDER, op, False, None, int((time.time() - t0) * 1000),
                     note=str(e)[:200])
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"{LLM_PROVIDER} 호출 실패: {last}")


def _openai(conn, path, payload, op):
    """OpenAI 호환 호출 (bizrouter 전부, gemini 의 생성)."""
    base = f"{GEMINI_BASE}/openai" if LLM_PROVIDER == "gemini" else BIZROUTER_BASE
    return _post(conn, f"{base}{path}", payload, op,
                 {"Authorization": f"Bearer {_key()}", "Content-Type": "application/json"})


def embed(conn, texts, model=None, dim=None, query=False):
    """텍스트 리스트 → 벡터 리스트. 순서는 입력과 같게 맞춰 돌려준다.

    query=True 는 '검색 질문' 이라는 뜻. Gemini 는 문서와 질문을 다르게 임베딩하면 검색이 낫다.
    """
    model = model or EMBED_MODEL
    dim = dim or EMBED_DIM
    if LLM_PROVIDER == "gemini":
        name = model if model.startswith("models/") else f"models/{model}"
        task = "RETRIEVAL_QUERY" if query else "RETRIEVAL_DOCUMENT"
        data = _post(conn, f"{GEMINI_BASE}/{name}:batchEmbedContents", {
            "requests": [{"model": name, "content": {"parts": [{"text": t}]},
                          "taskType": task, "outputDimensionality": dim} for t in texts],
        }, "embeddings", {"x-goog-api-key": _key(), "Content-Type": "application/json"})
        vecs = [e["values"] for e in data.get("embeddings", [])]
    else:
        data = _openai(conn, "/embeddings", {"model": model, "input": texts, "dimensions": dim},
                       "embeddings")
        rows = sorted(data["data"], key=lambda x: x.get("index", 0))
        vecs = [r["embedding"] for r in rows]
    if len(vecs) != len(texts):
        raise RuntimeError(f"임베딩 개수 불일치: 요청 {len(texts)} 응답 {len(vecs)}")
    got = len(vecs[0])
    if got != dim:
        raise RuntimeError(f"차원 불일치: 요청 {dim} 응답 {got}. "
                           "pgvector HNSW 는 2000차원까지만 인덱싱됩니다.")
    return vecs


RERANK_PROMPT = """질문과 장소 설명 목록이 주어진다. 질문에 맞는 순서대로 장소 번호를 고른다.
- 질문의 조건(실내/아이/반려동물/지역/분위기 등)에 실제로 맞는 곳을 위로.
- 설명에 근거가 없는 것은 추측하지 않는다.
- 상위 {n}개만, 관련도 점수(0~1)와 함께.
JSON 만 출력: {{"ranking":[{{"i":3,"score":0.92}}, ...]}}"""


def rerank(conn, query, documents, top_n=5, model=None):
    """[{index, relevance_score}] — 관련도 높은 순."""
    if LLM_PROVIDER != "gemini":
        data = _openai(conn, "/rerank", {
            "model": model or RERANK_MODEL, "query": query,
            "documents": documents, "top_n": top_n,
        }, "rerank")
        return data.get("results") or data.get("data") or []

    # Gemini: 리랭커 대신 생성 모델에게 한 번에 읽히고 순서를 받는다
    flat = [re.sub(r"\s+", " ", d)[:300] for d in documents]
    listing = "\n".join(f"[{i}] {d}" for i, d in enumerate(flat))
    text = chat(conn, [{"role": "system", "content": RERANK_PROMPT.format(n=top_n)},
                       {"role": "user", "content": f"질문: {query}\n\n{listing}"}],
                model=model or RERANK_MODEL, temperature=0, op="rerank",
                reasoning_effort="low")   # Gemini 3 은 생각 단계를 끌 수 없어 낮춘다 (기본값이면 20~30초)
    t = re.sub(r"```(?:json)?", "", text or "")
    i, j = t.find("{"), t.rfind("}")
    ranking = json.loads(t[i:j + 1]).get("ranking", []) if 0 <= i < j else []
    out, seen = [], set()
    for r in ranking:
        try:
            idx = int(r.get("i"))
        except (TypeError, ValueError, AttributeError):
            continue
        if 0 <= idx < len(documents) and idx not in seen:     # 목록 밖 번호·중복은 버린다
            seen.add(idx)
            out.append({"index": idx, "relevance_score": float(r.get("score") or 0)})
    return out[:top_n]


def chat(conn, messages, model=None, op="chat", **kw):
    data = _openai(conn, "/chat/completions", {
        "model": model or CHAT_MODEL, "messages": messages, **kw,
    }, op)
    return data["choices"][0]["message"]["content"]


def to_pgvector(v):
    """psycopg 로 vector 컬럼에 넣을 문자열."""
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"
