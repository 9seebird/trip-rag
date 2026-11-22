"""검색어 손질 — 줄임말 넓히기와 '데이터에 없는 말' 알아채기.

관광공사 데이터는 공식 소개글이라 '가맥', '카공' 같은 말이 안 나온다. 그대로 찾으면
의미 검색이 그나마 비슷해 보이는 걸 억지로 가져온다 ('전주 가맥집' → 가정식 백반).

  1. 줄임말 넓히기  : 사전에 있는 말이면 뜻이 같은 말을 검색어 뒤에 붙여 같이 찾는다.
  2. 없는 말 알아채기: 질문의 낱말이 전국 어느 장소의 이름·소개글에도 없으면 '없는 말' 이다.
                      낱말이 전부 없는 말이면 결과를 믿을 수 없다(weak) → 화면이 결과를 접고 솔직하게 말한다.

DB 에 묻는 부분(has_any)은 search.py 가 넘겨준다. 여기는 DB·API 없이 돈다 (eval/test_vocab.py).
"""

# 줄임말·신조어 → 데이터에 실제로 나오는 말. 새 말은 한 줄 추가.
# 키는 조사 뗀 낱말 기준이다. '가맥집', '카공하기' 처럼 끝꼴(ENDINGS)이 붙은 것도 같이 잡는다.
SLANG = {
    "가맥":     ["맥주", "호프", "주점", "포차"],
    "카공":     ["카페", "스터디"],
    "노포":     ["오래된", "전통", "대를"],
    "빵지순례": ["빵", "베이커리", "제과"],
    "빵투어":   ["빵", "베이커리", "제과"],
    "인생샷":   ["포토존", "사진", "전망"],
    "뷰맛집":   ["전망", "경치", "풍경"],
    "오션뷰":   ["바다", "전망", "해변"],
    "차박":     ["캠핑", "오토캠핑", "야영"],
    "혼밥":     ["1인", "혼자"],
    "핫플":     ["인기", "명소"],
}


# 낱말 끝에 붙는 꼴. '가맥집', '카공하기', '데이트하러' 를 '가맥', '카공', '데이트' 로 본다.
# 앞에서 아무렇게나 잘라 보면 '전일갑오' 가 '전일' 로 데이터에 걸려 버리므로 정해진 꼴만 뗀다.
ENDINGS = ("하기", "하는", "하러", "하고", "할", "집", "기", "러")


def stems(t):
    """낱말과, 끝꼴을 뗀 꼴 (뗀 뒤 2글자 이상일 때만)."""
    out = [t]
    for e in ENDINGS:
        if t.endswith(e) and len(t) - len(e) >= 2:
            out.append(t[: -len(e)])
            break
    return out


def slang_words(t):
    """낱말이 사전에 있으면 넓힐 말 목록, 없으면 None."""
    for s in stems(t):
        if s in SLANG:
            return SLANG[s]
    return None


def content_tokens(toks, why):
    """조건으로 이미 쓰인 말(지역 '전주', '실내' 등)을 뺀 나머지 = 무엇을 찾는지 말하는 낱말."""
    matched = [w.get("matched") or "" for w in why]
    used = " ".join(matched)
    # '광명시' 는 지역 조건 '광명' 으로 이미 쓰였다 (조건 낱말로 시작하는 꼴도 뺀다)
    return [t for t in toks if t not in used and not any(m and t.startswith(m) for m in matched)]


def judge(toks, has_any):
    """toks: content_tokens 결과. has_any(words) → 그중 하나라도 데이터에 있으면 True.

    돌려주는 것
      expanded : {낱말: [넓힌 말]}  — 검색어 뒤에 붙일 것
      absent   : [그 말 그대로는 데이터에 없는 낱말] — 넓혀 찾았어도 진짜 그 가게는 없을 수 있다 ('가맥집')
      unknown  : [넓힌 말까지 다 없는 낱말]
      weak     : 찾는 낱말이 있는데 전부 unknown → 결과를 믿을 수 없음
    """
    expanded, absent, unknown = {}, [], []
    for t in toks:
        extra = slang_words(t)
        if extra:
            expanded[t] = extra
        if not has_any(stems(t)):
            absent.append(t)
            if not (extra and has_any(extra)):
                unknown.append(t)
    weak = bool(toks) and len(unknown) == len(toks)
    return {"expanded": expanded, "absent": absent, "unknown": unknown, "weak": weak}


def search_text(q, expanded):
    """검색에 쓸 문장: 원래 질문 + 넓힌 말."""
    extra = [w for ws in expanded.values() for w in ws]
    return q + (" " + " ".join(extra) if extra else "")
