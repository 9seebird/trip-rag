"""반나절 코스 짜기 (설계문서 5-5).

원칙: **후보는 코드가 고르고, 모델은 후보 안에서 순서와 이유만 쓴다.**
모델에게 "제주 코스 짜줘" 라고만 하면 없는 장소를 지어낸다.

    기준 장소
     └→ 칸(slot)별 후보   코드. 반경 안에서 유형별로 가까운 4곳씩 (없으면 반경을 넓힌다. 남은 후보는 화면의 바꾸기용)
         └→ 고르기·순서   모델. 칸마다 1곳, 방문 순서, 한 줄 이유 (JSON)
             └→ 검증       코드. 후보 밖 id·중복·빠진 칸이 있으면 모델 결과를 버리고 규칙으로 대체
                 └→ 계산   코드. 구간 거리·이동 시간·체류 시간, 그리고 '최단 순서' 와 비교

거리는 직선거리(Haversine)에 도로 보정 1.3 을 곱해 차로 이동 시간을 어림한다.
지도 API 를 붙이면 이 부분만 바꾸면 된다.
"""
import itertools
import json
import math
import re
import time

from ingest.bizrouter import chat

from .extract import AREA_NAME

TYPE_NAME = {12: "관광지", 14: "문화시설", 15: "축제공연행사", 28: "레포츠", 39: "음식점"}

ROAD_FACTOR = 1.3      # 직선 → 도로 거리 어림
CAR_KMH = 40           # 기본값 (지역별 값이 없을 때)
# 지역별 차 평균 속도 어림. 큰 도시는 시내 정체가 잦아 느리게, 도 단위는 국도 거리가 길어 빠르게 본다.
AREA_KMH = {
    1: 20, 2: 25, 3: 30, 4: 30, 5: 30, 6: 25, 7: 30, 8: 35,      # 특별·광역시
    31: 35, 32: 45, 33: 45, 34: 45, 35: 45, 36: 40, 37: 45, 38: 45, 39: 40,
}
# 모델 응답을 기다리는 최대 시간(초). 넘으면 규칙(가까운 곳 + 최단 순서)으로 바로 짠다.
# Gemini 무료 등급은 한 번에 10~40초가 걸리기도 해서 둔 상한.
LLM_TIMEOUT = 12.0
CAFE_CAT3 = "A05020900"
CAFE_RE = r"(카페|커피|cafe|coffee|베이커리|디저트)"

# 칸: (이름, 사람이 읽을 이름, 유형, 카페 여부(None=상관없음), 체류 분)
SLOTS = {
    "sight": ("볼거리", [12, 14, 28], None, 60),
    "meal":  ("식사",   [39],         False, 60),
    "cafe":  ("카페",   [39],         True,  40),
}
TEMPLATE = ["sight", "meal", "cafe"]


# ── 순수 계산 (DB·모델 없이 테스트 가능) ─────────────────────────────────────────
def haversine(a_lat, a_lon, b_lat, b_lon):
    r = 6371.0
    p1, p2 = math.radians(a_lat), math.radians(b_lat)
    dp, dl = p2 - p1, math.radians(b_lon - a_lon)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def drive_min(km, kmh=CAR_KMH):
    """직선 km → 차로 분 (5분 단위 올림, 최소 5분)."""
    m = km * ROAD_FACTOR / kmh * 60
    return max(5, int(math.ceil(m / 5.0)) * 5)


def route_km(anchor, stops):
    pts = [anchor] + list(stops)
    return sum(haversine(a["lat"], a["lon"], b["lat"], b["lon"]) for a, b in zip(pts, pts[1:]))


def best_order(anchor, stops):
    """기준 장소에서 출발하는 최단 순서. 칸이 3개라 전수 비교(6가지)로 충분하다."""
    return list(min(itertools.permutations(stops), key=lambda p: route_km(anchor, p)))


def slot_of(place):
    """장소가 어느 칸에 해당하는지."""
    if place["content_type"] == 39:
        return "cafe" if is_cafe(place) else "meal"
    return "sight"


def is_cafe(place):
    return place.get("cat3") == CAFE_CAT3 or bool(re.search(CAFE_RE, place.get("title") or "", re.I))


def template_for(anchor):
    """기준 장소와 같은 칸은 빼고, 그 자리를 볼거리로 채운다. 항상 3칸."""
    kind = slot_of(anchor)
    slots = [s for s in TEMPLATE if s != kind]
    while len(slots) < 3:
        slots.insert(0, "sight")
    # 같은 칸이 둘이면 이름을 갈라 둔다 (sight, sight2)
    out, seen = [], {}
    for s in slots:
        seen[s] = seen.get(s, 0) + 1
        out.append(s if seen[s] == 1 else f"{s}{seen[s]}")
    return out


def parse_json(text):
    """모델 응답에서 JSON 한 덩어리를 꺼낸다. ```json 울타리, 앞뒤 잡담을 견딘다."""
    if not text:
        return None
    t = re.sub(r"```(?:json)?", "", text)
    i, j = t.find("{"), t.rfind("}")
    if i < 0 or j <= i:
        return None
    try:
        return json.loads(t[i:j + 1])
    except json.JSONDecodeError:
        return None


def validate(picks, cands):
    """모델이 고른 것이 후보 안에 있는지 — 이 프로젝트에서 '근거 충실도' 를 재는 지점.

    picks : [{"id": .., "reason": ..}, ...]  방문 순서
    cands : {slot: [place, ...]}
    반환  : (칸별로 정리된 선택 [(slot, place, reason)] 순서 유지, 문제 목록)
    """
    issues = []
    where = {p["content_id"]: s for s, ps in cands.items() for p in ps}
    by_id = {p["content_id"]: p for ps in cands.values() for p in ps}
    chosen, used_slots, used_ids = [], set(), set()
    for x in picks or []:
        try:
            cid = int(x.get("id"))
        except (TypeError, ValueError, AttributeError):
            issues.append(f"id 형식 오류: {x!r}"[:80]); continue
        if cid not in by_id:
            issues.append(f"후보에 없는 장소 {cid}"); continue
        if cid in used_ids:
            issues.append(f"중복 {cid}"); continue
        s = where[cid]
        if s in used_slots:
            issues.append(f"'{s}' 칸에서 두 곳을 고름"); continue
        used_ids.add(cid); used_slots.add(s)
        chosen.append((s, by_id[cid], (x.get("reason") or "").strip()[:120]))
    missing = [s for s, ps in cands.items() if ps and s not in used_slots]
    if missing:
        issues.append(f"빠진 칸: {', '.join(missing)}")
    return chosen, issues


def fallback(anchor, cands):
    """모델 없이 규칙으로: 칸마다 가장 가까운 곳, 순서는 최단."""
    picks = [(s, ps[0], f"가장 가까운 {SLOTS[s.rstrip('0123456789')][0]}") for s, ps in cands.items() if ps]
    by_id = {p["content_id"]: (s, p, r) for s, p, r in picks}
    order = best_order(anchor, [p for _, p, _ in picks])
    return [by_id[p["content_id"]] for p in order]


# ── DB ──────────────────────────────────────────────────────────────────────────
def _place(conn, cid):
    return conn.execute(
        "SELECT content_id, content_type, title, addr, lat, lon, image_url, overview, cat3,"
        " indoor, baby_ok, area_code FROM places WHERE content_id = %s", (cid,)).fetchone()


def candidates(conn, anchor, slot, radius, exclude, k=4):
    base = slot.rstrip("0123456789")
    _, types, cafe, _ = SLOTS[base]
    cafe_sql = ""
    if cafe is True:
        cafe_sql = "AND (cat3 = %(cafe_cat)s OR title ~* %(cafe_re)s)"
    elif cafe is False:
        cafe_sql = "AND NOT (coalesce(cat3,'') = %(cafe_cat)s OR title ~* %(cafe_re)s)"
    sql = f"""
    SELECT * FROM (
      SELECT content_id, content_type, title, addr, lat, lon, image_url, overview, cat3, indoor, baby_ok,
        6371 * 2 * asin(sqrt(
          power(sin(radians(lat - %(lat)s) / 2), 2) +
          cos(radians(%(lat)s)) * cos(radians(lat)) * power(sin(radians(lon - %(lon)s) / 2), 2))) AS km
      FROM places
      WHERE content_type = ANY(%(types)s) AND lat IS NOT NULL
        AND NOT (content_id = ANY(%(exclude)s)) {cafe_sql}
    ) t WHERE km <= %(radius)s ORDER BY km LIMIT %(k)s
    """
    return conn.execute(sql, dict(lat=anchor["lat"], lon=anchor["lon"], types=types,
                                  exclude=list(exclude), radius=radius, k=k,
                                  cafe_cat=CAFE_CAT3, cafe_re=CAFE_RE)).fetchall()


def gather(conn, anchor, radius):
    """칸별 후보. 칸마다 반경을 따로 넓힌다 (식당은 가까운데 카페만 먼 경우가 있다)."""
    cands, radii, exclude = {}, {}, {anchor["content_id"]}
    for slot in template_for(anchor):
        rows = []
        for r in (radius, radius * 2, radius * 4):
            rows = candidates(conn, anchor, slot, r, exclude)
            if rows:
                radii[slot] = r
                break
        cands[slot] = [dict(x) for x in rows]
        exclude |= {x["content_id"] for x in rows}
    return cands, radii


# ── 모델 ────────────────────────────────────────────────────────────────────────
PROMPT = """너는 국내 여행 코스를 짜는 도우미다. 지역은 입력의 area 에 있다.
출발지와 칸별 후보가 주어진다. 규칙:
- 각 칸에서 정확히 1곳씩 고른다. 후보 목록에 있는 id 만 쓴다. 새 장소를 지어내지 않는다.
- 출발지에서 시작해 이동이 자연스러운 방문 순서를 정한다. 식사는 너무 늦게 두지 않는다.
- 각 장소마다 왜 넣었는지 한 문장(40자 이내)으로 쓴다. 후보 설명에 없는 사실을 만들지 않는다.
- 전체 코스를 한 문장으로 요약한다.
JSON 만 출력한다:
{"stops":[{"id":123,"reason":"..."}, ...], "summary":"..."}
stops 는 출발지를 빼고 방문 순서대로."""


def _brief(p, anchor):
    km = haversine(anchor["lat"], anchor["lon"], p["lat"], p["lon"])
    ov = re.sub(r"\s+", " ", p.get("overview") or "")[:120]
    return {"id": p["content_id"], "title": p["title"], "type": TYPE_NAME.get(p["content_type"]),
            "km_from_start": round(km, 1), "desc": ov}


def ask_model(conn, anchor, cands):
    payload = {
        "area": AREA_NAME.get(anchor.get("area_code"), "국내"),
        "start": {"title": anchor["title"], "type": TYPE_NAME.get(anchor["content_type"]),
                  "desc": re.sub(r"\s+", " ", anchor.get("overview") or "")[:120]},
        "slots": {SLOTS[s.rstrip("0123456789")][0] + (s[-1] if s[-1].isdigit() else ""):
                  [_brief(p, anchor) for p in ps] for s, ps in cands.items() if ps},
    }
    text = chat(conn, [{"role": "system", "content": PROMPT},
                       {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
                temperature=0.3, post_kw=dict(timeout=LLM_TIMEOUT, retries=1, wait_429=False))
    return parse_json(text), text


# ── 조립 ────────────────────────────────────────────────────────────────────────
def _stop(p, slot, reason, prev, kmh=CAR_KMH):
    km = haversine(prev["lat"], prev["lon"], p["lat"], p["lon"])
    base = slot.rstrip("0123456789")
    ov = p.get("overview") or ""
    return {
        "content_id": p["content_id"], "title": p["title"],
        "type": TYPE_NAME.get(p["content_type"], str(p["content_type"])),
        "slot": base, "slot_key": slot, "slot_name": SLOTS[base][0],
        "content_type": p["content_type"],
        "addr": p.get("addr"), "image": p.get("image_url"), "lat": p["lat"], "lon": p["lon"],
        "indoor": p.get("indoor"), "baby_ok": p.get("baby_ok"),
        "snippet": (ov[:120] + "…") if len(ov) > 120 else ov,
        "reason": reason,
        "leg_km": round(km, 1), "leg_min": drive_min(km, kmh), "stay_min": SLOTS[base][3],
    }


def build(conn, content_id, radius=5.0, use_llm=True):
    t0 = time.time()
    anchor = _place(conn, content_id)
    if not anchor:
        return {"error": f"장소 {content_id} 없음"}
    anchor = dict(anchor)
    if anchor["lat"] is None:
        return {"error": "좌표가 없는 장소라 코스를 짤 수 없습니다"}

    cands, radii = gather(conn, anchor, radius)
    t1 = time.time()

    source, issues, summary, raw = "rule", [], "", None
    chosen = []
    if use_llm and any(cands.values()):
        try:
            data, raw = ask_model(conn, anchor, cands)
            if data is None:
                issues.append("모델 응답이 JSON 이 아님")
            else:
                chosen, issues = validate(data.get("stops"), cands)
                summary = (data.get("summary") or "").strip()[:160]
            # 후보 밖 id·중복은 그 항목만 버리고 기록한다. 칸이 비면 모델 결과 전체를 버린다.
            if chosen and not any(i.startswith("빠진 칸") for i in issues):
                source = "llm"
        except Exception as e:                       # 모델이 죽어도 코스는 나와야 한다
            issues.append(f"모델 호출 실패: {str(e)[:120]}")
    if source != "llm":
        chosen = fallback(anchor, cands)
        summary = ""
    t2 = time.time()

    kmh = AREA_KMH.get(anchor.get("area_code"), CAR_KMH)
    stops, prev = [], anchor
    for slot, p, reason in chosen:
        stops.append(_stop(p, slot, reason, prev, kmh))
        prev = p
    picked = [p for _, p, _ in chosen]
    best = best_order(anchor, picked) if picked else []

    move_min = sum(s["leg_min"] for s in stops)
    stay_min = 60 + sum(s["stay_min"] for s in stops)     # 출발지 체류 60분 포함
    return {
        "area": AREA_NAME.get(anchor.get("area_code")),
        "start": _stop(anchor, slot_of(anchor), "출발지", anchor) | {"leg_km": 0, "leg_min": 0},
        "stops": stops,
        "summary": summary,
        "source": source,                     # llm | rule
        "issues": issues,                     # 후보 밖 id·중복·빠진 칸 (근거 충실도)
        # 칸마다 후보 전부 (화면의 '다른 곳으로 바꾸기' 가 쓴다). 구간 거리는 화면이 순서에 맞춰 다시 계산한다
        "candidates": {s: [_stop(p, s, "", anchor, kmh) | {"km": round(p["km"], 1)} for p in ps]
                       for s, ps in cands.items()},
        "kmh": kmh, "road_factor": ROAD_FACTOR,
        "radius_km": radii,
        "totals": {
            "move_km": round(route_km(anchor, picked), 1),
            "best_km": round(route_km(anchor, best), 1),     # 같은 장소들의 최단 순서
            "move_min": move_min, "stay_min": stay_min, "total_min": move_min + stay_min,
        },
        "took_ms": {"candidates": int((t1 - t0) * 1000), "model": int((t2 - t1) * 1000)},
    }
