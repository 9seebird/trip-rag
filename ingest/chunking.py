"""청킹 — overview 를 임베딩할 조각으로 자른다.

원칙 두 가지
  1. 조각 앞에 제목과 유형을 붙인다. "성산일출봉" 이라는 단어가 조각 안에 없으면
     고유명사 질문에서 안 걸린다. 설계문서 4장의 그 이야기.
  2. 문장 경계에서 자른다. 글자 수로 뚝 자르면 문맥이 끊겨 유사도가 떨어진다.

대부분의 overview 가 500~1,500자라 장소당 1~3조각이 나온다.
"""
import re

TYPE_NAME = {12: "관광지", 14: "문화시설", 15: "축제공연행사", 28: "레포츠", 39: "음식점"}
SENT = re.compile(r"(?<=[.!?。])\s+|\n+")

MAX_CHARS = 1000
OVERLAP = 120
MIN_TAIL = 200          # 마지막 조각이 이보다 짧으면 앞 조각에 붙인다


def header(place):
    """조각마다 앞에 붙는 머리말. 지역명을 넣어 두면 고유명사 검색에 도움이 된다."""
    bits = [f"[{place['title']}]"]
    t = TYPE_NAME.get(place["content_type"])
    if t:
        bits.append(t)
    addr = (place.get("addr") or "").split()
    if addr:
        bits.append(" ".join(addr[:2]))     # "제주특별자치도 서귀포시"
    return " ".join(bits)


def split(text, max_chars=MAX_CHARS, overlap=OVERLAP):
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    sents = [s.strip() for s in SENT.split(text) if s and s.strip()]
    out, cur = [], ""
    for s in sents:
        if len(s) > max_chars:                      # 문장 하나가 너무 길면 강제로 자른다
            if cur:
                out.append(cur); cur = ""
            for i in range(0, len(s), max_chars - overlap):
                out.append(s[i:i + max_chars])
            continue
        if len(cur) + len(s) + 1 <= max_chars:
            cur = f"{cur} {s}".strip()
        else:
            out.append(cur)
            tail = cur[-overlap:] if overlap else ""
            cur = f"{tail} {s}".strip()
    if cur:
        if out and len(cur) < MIN_TAIL:
            out[-1] = f"{out[-1]} {cur}"[-max_chars * 2:]
        else:
            out.append(cur)
    return out


def chunks_for(place, max_chars=MAX_CHARS, overlap=OVERLAP, with_header=True):
    """(seq, text) 리스트. text 는 임베딩에 그대로 들어간다.

    with_header=False 는 '제목 접두가 실제로 효과가 있는가' 를 재기 위한 대조군이다.
    """
    parts = split(place.get("overview"), max_chars, overlap)
    if not parts:
        return []
    if not with_header:
        return list(enumerate(parts))
    h = header(place)
    return [(i, f"{h} {p}") for i, p in enumerate(parts)]
