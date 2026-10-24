"""검색 파이프라인.

    질문
     └→ 조건 뽑기 (규칙)        지역·실내·반려동물·아이·유형
         └→ 조건 필터 (SQL)      먼저 좁힌다. 반대로 하면 조건 밖의 것이 상위에 온다
             └→ 벡터 검색 top N  의미로 후보를 넓게
                 └→ 리랭커 top k 질문과 문서를 같이 읽고 다시 줄세우기

키워드 검색은 비교 화면의 대조군이다. 대조군을 일부러 약하게 만들면 비교가 의미를 잃으므로
조사·어미를 제대로 털어서 공정하게 맞춘다.
"""
import math
import re
import time

from ingest.bizrouter import embed, rerank, to_pgvector
from ingest.config import EMBED_MODEL

from .extract import extract

# 서비스 화면에서 리랭크를 기다리는 최대 시간(초). 넘으면 벡터 순서로 보여준다.
# Gemini 생성 모델로 순위를 매기면 20~40초가 걸리기도 해서 둔 상한.
RERANK_TIMEOUT = 5.0

TYPE_NAME = {12: "관광지", 14: "문화시설", 15: "축제공연행사", 28: "레포츠", 39: "음식점"}

# 긴 것부터 떼야 한다 ("에서" 를 "에" 로 먼저 떼면 "서" 가 남는다)
JOSA = ["으로부터", "에서부터", "이랑", "에서", "으로", "에게", "한테", "부터", "까지",
        "보다", "처럼", "같이", "하고", "이나", "의", "은", "는", "이", "가", "을", "를",
        "에", "와", "과", "도", "만", "랑", "로"]

# 검색어로서 뜻이 없는 말들. 이걸 안 털면 "있는" 이 온 데이터에 다 걸린다.
STOP = {
    "있는", "없는", "하는", "가는", "오는", "되는", "좋은", "괜찮은", "만한", "갈만한",
    "데리고", "가지고", "다니는", "즐기는", "먹는", "보는", "놀만한", "지내는", "쉬는",
    "곳", "장소", "데", "추천", "때", "날", "정도", "느낌", "분위기", "여기", "거기",
    "가능한", "괜찮", "어디", "무엇", "뭐", "좀", "같은", "위한", "위해",
}


def tokens(q):
    out = []
    for raw in re.split(r"[\s,·/]+", (q or "").strip()):
        t = re.sub(r"[^0-9A-Za-z가-힣]", "", raw)
        if not t:
            continue
        if t in STOP:
            continue
        for j in JOSA:                       # 조사 떼기 (떼고도 2글자 이상일 때만)
            if t.endswith(j) and len(t) - len(j) >= 2:
                t = t[: -len(j)]
                break
        if len(t) >= 2 and t not in STOP:
            out.append(t)
    return list(dict.fromkeys(out))          # 중복 제거, 순서 유지


def _where(f):
    """조건 → SQL.

    파생·수집값(indoor, pet_allowed)은 **'아니다'로 확인된 것만 뺀다.**
    값이 비어 있는 곳까지 빼면, 아직 수집 안 한 컬럼에 필터를 거는 순간 결과가 0건이 된다.
    (2차 평가 q01·q03·q06 — detailIntro2 를 받기 전이라 baby_ok·pet_allowed 가 전부 NULL 이었다)
    모르는 건 남겨 두고 순위는 의미 검색·리랭커에 맡긴다.

    지역·유형은 원본에 항상 있는 값이라 그대로 건다.

    '아이 동반'(baby)은 **거르지 않는다.** 재료인 baby_ok 가 '유모차 대여/동반 가능' 이라서다.
    유모차를 못 끌어도 아이랑 가는 체험 박물관은 많다. 4차 평가에서 이 조건으로 거르자
    q06(아이랑 체험할 수 있는 박물관)의 정답이 빠졌다. 그래서 조건은 뽑되 결과 카드에 유모차 정보만 보여준다.
    """
    where, params = [], {}
    if f.get("area"):
        where.append("p.area_code = %(area)s"); params["area"] = f["area"]
    if f.get("types"):
        where.append("p.content_type = ANY(%(types)s)"); params["types"] = list(f["types"])
    if f.get("indoor") is not None:
        where.append("p.indoor IS DISTINCT FROM %(not_indoor)s"); params["not_indoor"] = not f["indoor"]
    if f.get("pet"):
        where.append("p.pet_allowed IS NOT FALSE")
    if f.get("near"):
        # 내 위치 반경. 위경도 사각형으로 먼저 좁히고(인덱스) 정확한 거리로 자른다
        lat, lon, r = f["near"]
        dlat, dlon = r / 111.0, r / (111.0 * max(0.2, math.cos(math.radians(lat))))
        where.append("p.lat BETWEEN %(nlat0)s AND %(nlat1)s AND p.lon BETWEEN %(nlon0)s AND %(nlon1)s")
        where.append(f"{_KM_SQL} <= %(nr)s")
        params.update(nlat=lat, nlon=lon, nr=r, nlat0=lat - dlat, nlat1=lat + dlat,
                      nlon0=lon - dlon, nlon1=lon + dlon)
    if f.get("when"):
        # 축제 기간이 질문의 기간과 겹치는 것만. 기간을 모르는 축제는 남긴다
        where.append("(p.content_type <> 15 OR (coalesce(p.event_start, '-infinity'::date) <= %(we)s"
                     " AND coalesce(p.event_end, 'infinity'::date) >= %(ws)s))")
        params.update(ws=f["when"][0], we=f["when"][1])
    # 이미 끝난 축제는 언제나 뺀다
    where.append("(p.content_type <> 15 OR p.event_end IS NULL OR p.event_end >= (now() AT TIME ZONE 'Asia/Seoul')::date)")
    return where, params


_KM_SQL = """6371 * 2 * asin(sqrt(
    power(sin(radians(p.lat - %(nlat)s) / 2), 2) +
    cos(radians(%(nlat)s)) * cos(radians(p.lat)) * power(sin(radians(p.lon - %(nlon)s) / 2), 2)))"""


def km_between(a_lat, a_lon, b_lat, b_lon):
    p1, p2 = math.radians(a_lat), math.radians(b_lat)
    h = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(b_lon - a_lon) / 2) ** 2)
    return 2 * 6371 * math.asin(math.sqrt(h))


def _row(r):
    ov = r.get("overview") or ""
    return {
        "content_id": r["content_id"],
        "title": r["title"],
        "type": TYPE_NAME.get(r["content_type"], str(r["content_type"])),
        "content_type": r["content_type"],
        "addr": r.get("addr"),
        "image": r.get("image_url"),
        "lat": r.get("lat"), "lon": r.get("lon"),
        "indoor": r.get("indoor"),
        "baby_ok": r.get("baby_ok"),          # 유모차 가능/불가 — 거르지 않고 표시만
        "pet_allowed": r.get("pet_allowed"),
        "snippet": (ov[:160] + "…") if len(ov) > 160 else ov,
        "area_code": r.get("area_code"),
        # 상세 보기용 (서비스 화면). 비교 화면은 안 쓴다.
        "overview": ov,
        "usetime": r.get("usetime"), "restdate": r.get("restdate"),
        "parking": r.get("parking"), "usefee": r.get("usefee"),
        "tel": r.get("tel") or r.get("infocenter"), "menu": r.get("menu"),
        "homepage": r.get("homepage"),
        "event_start": r.get("event_start"), "event_end": r.get("event_end"),
    }


def keyword_search(conn, q, limit=8, filters=None):
    ts = tokens(q)
    if not ts:
        return []
    where, params = _where(filters or {})
    params.update(toks=ts, q=q, limit=limit)
    sql = f"""
    WITH scored AS (
      SELECT p.*,
        (SELECT count(*) FROM unnest(%(toks)s::text[]) t WHERE p.title ILIKE '%%'||t||'%%') * 3
      + (SELECT count(*) FROM unnest(%(toks)s::text[]) t
         WHERE coalesce(p.overview,'') ILIKE '%%'||t||'%%') AS hits,
        similarity(p.title, %(q)s) AS sim
      FROM places p
      {"WHERE " + " AND ".join(where) if where else ""}
    )
    SELECT * FROM scored WHERE hits > 0 ORDER BY hits DESC, sim DESC, title LIMIT %(limit)s
    """
    out = []
    for r in conn.execute(sql, params).fetchall():
        d = _row(r); d["hits"] = int(r["hits"]); out.append(d)
    return out


def vector_candidates(conn, q, pool=30, filters=None, model=EMBED_MODEL):
    """model 은 chunks 안의 벡터 묶음을 가리키는 '이름표'다.

    실험용 변형은 '모델명#변형' 으로 저장하므로, 질문을 임베딩할 때는 앞부분만 쓴다.
    (이름표를 그대로 API 에 보내면 model_not_found 가 난다.)
    """
    qv = embed(conn, [q], model.split("#")[0], query=True)[0]
    where, params = _where(filters or {})
    where.append("c.model = %(model)s")
    params.update(model=model, qv=to_pgvector(qv), pool=pool)
    sql = f"""
    SELECT p.*, c.text AS chunk_text, c.embedding <=> %(qv)s::vector AS distance
    FROM chunks c JOIN places p ON p.content_id = c.content_id
    WHERE {" AND ".join(where)} ORDER BY distance LIMIT %(pool)s
    """
    seen, out = set(), []
    for r in conn.execute(sql, params).fetchall():
        if r["content_id"] in seen:
            continue
        seen.add(r["content_id"])
        d = _row(r)
        d["distance"] = round(float(r["distance"]), 4)
        d["similarity"] = round(1 - float(r["distance"]), 4)
        d["_text"] = r["chunk_text"]
        out.append(d)
    return out


def apply_rerank(conn, q, items, top_n=8, timeout=None):
    if not items:
        return []
    res = rerank(conn, q, [i["_text"] for i in items], top_n=top_n, timeout=timeout)
    out = []
    for rank, r in enumerate(res, 1):
        idx = r.get("index")
        if idx is None or idx >= len(items):
            continue
        it = dict(items[idx])
        it["rerank_score"] = round(float(r.get("relevance_score") or r.get("score") or 0), 4)
        it["was_rank"] = idx + 1
        it["rank"] = rank
        out.append(it)
    return out


def strip_private(items):
    return [{k: v for k, v in i.items() if not k.startswith("_")} for i in items]


HINT_ONLY = ("baby",)                 # 뽑기는 하되 SQL 로 거르지 않는 조건
SOFT_FILTERS = ("pet", "indoor", "types")   # 먼저 푸는 순서 (약한 조건부터)
MIN_CANDIDATES = 3


def filtered_candidates(conn, q, pool=30, filters=None, model=EMBED_MODEL):
    """조건 필터 + 벡터 검색. 후보가 말라붙으면 약한 조건부터 하나씩 푼다.

    화면(run)과 평가(run_eval)가 **반드시 이 함수 하나를 같이 탄다.**
    2차 평가에서는 평가가 이 단계를 건너뛰고 필터를 직접 걸어 실제보다 불리하게 쟀다.
    """
    filters = dict(filters or {})
    dropped = []
    cand = vector_candidates(conn, q, pool, filters, model)
    for soft in SOFT_FILTERS:
        if len(cand) >= MIN_CANDIDATES:
            break
        if filters.get(soft) not in (None, False):
            filters.pop(soft, None)
            dropped.append(soft)
            cand = vector_candidates(conn, q, pool, filters, model)
    return cand, filters, dropped


def run(conn, q, limit=8, compare="keyword", use_filters=True, pool=30, overrides=None):
    """compare='keyword' → 키워드 vs 의미 / compare='rerank' → 의미 vs 의미+리랭커"""
    ex = extract(q)
    filters = dict(ex["filters"]) if use_filters else {}
    if overrides:
        filters.update({k: v for k, v in overrides.items() if v is not None})

    t0 = time.time()
    cand, filters, dropped = filtered_candidates(conn, q, pool, filters)
    t1 = time.time()

    out = {
        "query": q,
        "tokens": tokens(q),
        "extracted": ex["why"],
        "filters": {k: v for k, v in filters.items() if v not in (None, False) and k not in HINT_ONLY},
        "hints": [k for k in HINT_ONLY if filters.get(k)],
        "dropped_filters": dropped,
        "took_ms": {},
    }

    vec = cand[:limit]
    out["vector"] = strip_private(vec)
    out["took_ms"]["vector"] = int((t1 - t0) * 1000)

    if compare == "rerank":
        t2 = time.time()
        out["reranked"] = strip_private(apply_rerank(conn, q, cand, limit))
        out["took_ms"]["rerank"] = int((time.time() - t2) * 1000)
        out["left_key"], out["right_key"] = "vector", "reranked"
    else:
        t2 = time.time()
        kws = keyword_search(conn, q, limit, filters if use_filters else None)
        out["keyword"] = kws
        out["took_ms"]["keyword"] = int((time.time() - t2) * 1000)
        out["left_key"], out["right_key"] = "keyword", "vector"

    left = {r["content_id"] for r in out[out["left_key"]]}
    right = {r["content_id"] for r in out[out["right_key"]]}
    out["only_in_right"] = sorted(right - left)
    out["overlap"] = len(left & right)
    return out


def find(conn, q, limit=12, pool=30, overrides=None, use_rerank=True, near=None):
    """서비스 화면용: 조건 필터 + 벡터 + 리랭커 한 줄. 비교용 대조군(키워드)은 돌리지 않는다.

    near = (위도, 경도, 반경km) 이면 그 반경 안에서만. 이때 지역 조건(질문 속 '부산' 등)은 쓰지 않는다.
    """
    ex = extract(q)
    filters = dict(ex["filters"])
    if overrides:
        filters.update({k: v for k, v in overrides.items() if v is not None})
    if near:
        filters["near"] = near
        filters.pop("area", None)
    t0 = time.time()
    cand, filters, dropped = filtered_candidates(conn, q, pool, filters)
    items = cand[:limit]
    if use_rerank and cand:
        try:
            items = apply_rerank(conn, q, cand, limit, timeout=RERANK_TIMEOUT)
        except Exception:                 # 리랭커가 죽어도 벡터 순서로는 보여준다
            items = cand[:limit]
    items = strip_private(items)
    if near:
        for it in items:
            if it.get("lat") is not None:
                it["dist_km"] = round(km_between(near[0], near[1], it["lat"], it["lon"]), 1)
    return {
        "query": q,
        "extracted": [w for w in ex["why"] if not (near and w["field"] == "area")],
        "filters": {k: v for k, v in filters.items()
                    if v not in (None, False) and k not in HINT_ONLY and k != "near"},
        "near": bool(near),
        "hints": [k for k in HINT_ONLY if filters.get(k)],
        "dropped_filters": dropped,
        "items": items,
        "took_ms": int((time.time() - t0) * 1000),
    }
