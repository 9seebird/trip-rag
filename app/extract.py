"""질문에서 조건 뽑기 (규칙 기반).

설계문서 5-2 의 두 방법 중 규칙 쪽. 빠르고 공짜다.
못 잡는 표현은 나중에 모델에 넘기고, 그 결과를 규칙과 비교해 README 에 쓴다.

핵심은 **의미로 찾을 것과 조건으로 걸 것을 나누는 일**이다.
"제주"를 벡터로 찾으면 강원도가 올라오고, "실내"를 벡터로 찾으면 카약이 올라온다.
"""
import re

RULES = [
    # (조건 이름, 값, 정규식, 사람이 읽을 이름)
    ("indoor", True,  r"(실내|비\s*오는|비\s*올|비가|우천|비\s*내리|눈\s*오는|더울\s*때|추울\s*때)", "실내"),
    ("indoor", False, r"(야외|실외|바깥|노천)", "실외"),
    ("pet",    True,  r"(강아지|반려견|애견|반려동물|펫|멍멍이)", "반려동물 동반"),
    ("baby",   True,  r"(아이|애기|아기|유아|어린이|아동|키즈|초등|가족)", "아이 동반"),
]

TYPE_RULES = [
    (r"(맛집|밥|식당|먹을|먹기|한끼|점심|저녁|회|고기)", [39], "음식점"),
    (r"(카페|커피|디저트|브런치)", [39], "카페·음식점"),
    (r"(박물관|미술관|전시|뮤지엄|문화)", [12, 14], "관광지·문화시설"),
    (r"(축제|행사|공연)", [15], "축제공연행사"),
    (r"(체험|액티비티|레포츠|카약|서핑|승마|골프)", [28], "레포츠"),
]

AREA_RULES = [(r"(제주|서귀포)", 39, "제주")]

# 조건으로 뺀 낱말은 의미 검색에서 빼도 되지만, 남겨두는 게 대체로 낫다.
# 그래서 기본은 원문 그대로 임베딩하고, strip=True 일 때만 지운다.


def extract(q, strip=False):
    q = q or ""
    found = {"area": None, "indoor": None, "pet": False, "baby": False, "types": None}
    why = []

    for name, value, pat, label in RULES:
        if found.get(name) not in (None, False):
            continue
        m = re.search(pat, q)
        if m:
            found[name] = value
            why.append({"field": name, "value": value, "label": label, "matched": m.group(0)})

    for pat, types, label in TYPE_RULES:
        m = re.search(pat, q)
        if m:
            found["types"] = types
            why.append({"field": "types", "value": types, "label": label, "matched": m.group(0)})
            break

    for pat, area, label in AREA_RULES:
        m = re.search(pat, q)
        if m:
            found["area"] = area
            why.append({"field": "area", "value": area, "label": label, "matched": m.group(0)})
            break

    text = q
    if strip:
        for w in why:
            text = text.replace(w["matched"], " ")
        text = re.sub(r"\s+", " ", text).strip() or q

    return {"filters": found, "why": why, "text": text}
