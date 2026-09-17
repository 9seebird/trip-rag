"""질문에서 조건 뽑기 (규칙 기반).

설계문서 5-2 의 두 방법 중 규칙 쪽. 빠르고 공짜다.
못 잡는 표현은 나중에 모델에 넘기고, 그 결과를 규칙과 비교해 README 에 쓴다.

핵심은 **의미로 찾을 것과 조건으로 걸 것을 나누는 일**이다.
"제주"를 벡터로 찾으면 강원도가 올라오고, "실내"를 벡터로 찾으면 카약이 올라온다.
"""
import re

# 한국어는 띄어쓰기로 낱말이 안 갈린다. "아이" 를 그냥 찾으면 "아이스뮤지엄" 에 걸리고,
# "아기" 는 "아기자기" 에, "회" 는 "사회"·"회색" 에 걸린다. (2차 평가에서 q01·q06·q20 이 이걸로 0점)
# 그래서 짧고 흔한 낱말은 뒤에 올 수 있는 것(조사·어미·끝)을 명시한다.
_JOSA = r"(?=$|\s|[,.!?·/]|랑|이랑|와|과|하고|들|를|을|가|이|는|은|도|의|에게|한테|같이|용|전용)"

RULES = [
    # (조건 이름, 값, 정규식, 사람이 읽을 이름)
    ("indoor", True,  r"(실내|비\s*오는|비\s*올|비가\s*오|비\s*내리|우천|눈\s*오는|더울\s*때|추울\s*때)", "실내"),
    ("indoor", False, r"(야외|실외|바깥|노천)", "실외"),
    ("pet",    True,  r"(강아지|반려견|애견|반려동물|멍멍이|(?<![가-힣])펫" + _JOSA + r")", "반려동물 동반"),
    ("baby",   True,  r"((?:아이|애기|아기)" + _JOSA +
                      r"|유아|어린이|아동|키즈|초등학생|꼬마|(?<![가-힣])애들" + _JOSA + r")", "아이 동반"),
]

TYPE_RULES = [
    (r"(맛집|식당|밥집|밥\s*먹|한\s*끼|점심|저녁\s*먹|먹을\s*곳|먹기\s*좋은|잘하는\s*집|횟집|물회|회덮밥|고기\s*집|흑돼지)",
     [39], "음식점"),
    (r"(카페|커피|디저트|브런치)", [39], "카페·음식점"),
    (r"(박물관|미술관|전시|뮤지엄|기념관|문화)", [12, 14], "관광지·문화시설"),
    (r"(축제|행사|공연)", [15], "축제공연행사"),
    # "체험" 은 레포츠만의 말이 아니다. 아이스뮤지엄·농장 체험은 관광지(12)로 분류돼 있다 (q14)
    (r"(체험)", [12, 14, 28], "관광지·문화시설·레포츠"),
    (r"(액티비티|레포츠|카약|서핑|승마|골프|패러글라이딩|스쿠버)", [28], "레포츠"),
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
