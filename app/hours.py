"""운영시간·쉬는 날 글 → 화면이 계산할 수 있는 값.

TourAPI 의 usetime / restdate 는 사람이 쓴 글이다.
    "매주 월요일 (공휴일인 경우 그 다음날)"   "매월 둘째, 넷째 주 화요일"   "연중무휴"
    "09:00~18:00 (입장마감 17:00)"           "하절기 09:00~19:00 / 동절기 09:00~18:00"
그대로는 '오늘 쉬나?' 를 못 묻는다. 확실히 읽히는 것만 뽑고, 애매하면 비워 둔다(모름).
틀린 '오늘 휴무' 는 사용자를 헛걸음하게 하지만, '모름' 은 원문을 보고 판단하게 할 뿐이라서.

    parse(usetime, restdate) → {"open": 540, "close": 1080,           # 분. 모르면 없음
                                "closed_dow": [0],                     # 매주 쉬는 요일 (0=월 … 6=일)
                                "closed_nth": [[2, 1], [4, 1]],        # 매월 n번째 요일 (n=-1 은 마지막)
                                "always_open": False}
요일 계산(그날이 휴무인가)은 화면이 한다 — 코스 날짜를 사용자가 바꿀 수 있어서.
"""
import re

DOW = {"월": 0, "화": 1, "수": 2, "목": 3, "금": 4, "토": 5, "일": 6}
NTH = {"첫째": 1, "첫번째": 1, "둘째": 2, "두번째": 2, "셋째": 3, "세번째": 3, "넷째": 4, "네번째": 4,
       "다섯째": 5, "마지막": -1}
_NTH_RE = "|".join(sorted(NTH, key=len, reverse=True))
_DAYLIST = r"([월화수목금토일](?:\s*[,·/및\s]\s*[월화수목금토일])*)\s*요일"
# 휴무가 아니라 예외를 말하는 괄호·덧붙임 ('(공휴일인 경우 다음날)', '단, …', '※ …')
_PAREN = re.compile(r"\([^)]*\)|（[^）]*）|단[,\s][^\n]*|※[^\n]*")
# '…인 경우 / …이면' 조건절의 시작. 그 앞까지만 휴무로 읽는다
#   '매주 월요일 공휴일인 경우 다음날' → '매주 월요일'   '월요일이 공휴일이면 개관' → ''
_COND = re.compile(r"[월화수목금토일]요일이|공휴일|명절|연휴|국경일|대체")
_COND_WORD = re.compile(r"경우|이면|라면|일시")
_ALWAYS = re.compile(r"(연중\s*무휴|휴무\s*없음|무휴|^\s*없음\s*$|365일|휴관일?\s*없음|정기\s*휴무\s*없음)")
_TIME = re.compile(r"(\d{1,2})\s*(?::|시)\s*(\d{2})?\s*분?\s*[~\-–∼〜]\s*(\d{1,2})\s*(?::|시)\s*(\d{2})?")


def _clean(s):
    return re.sub(r"<[^>]+>", " ", s or "").replace("&nbsp;", " ")


def closed_days(restdate):
    text = _clean(restdate).strip()
    if not text:
        return None
    if _ALWAYS.search(text):
        return {"always_open": True, "closed_dow": [], "closed_nth": []}
    segs = []
    for seg in re.split(r"[,\n]|<br\s*/?>", _PAREN.sub(" ", text)):
        if _COND_WORD.search(seg):
            m = _COND.search(seg)
            seg = seg[:m.start()] if m else ""
        segs.append(seg)
    body = ", ".join(segs)
    nth = []
    for m in re.finditer(rf"((?:(?:{_NTH_RE})\s*(?:주)?\s*[,·및\s]*\s*)+)(?:주\s*)?{_DAYLIST}", body):
        ns = [NTH[n] for n in re.findall(_NTH_RE, m.group(1))]
        ds = [DOW[d] for d in re.findall(r"[월화수목금토일]", m.group(2))]
        nth += [[n, d] for n in ns for d in ds]
        body = body.replace(m.group(0), " ")
    weekly = []
    for m in re.finditer(_DAYLIST, body):
        # '1월 1일' 처럼 날짜의 '일' 이 아니라 요일만 (바로 앞이 숫자면 날짜)
        start = m.start()
        if start and body[start - 1].isdigit():
            continue
        if re.match(r"\s*(오전|오후|\d)", body[m.end():]):      # '토요일 오후' 처럼 반나절만 쉬면 휴무로 안 본다
            continue
        weekly += [DOW[d] for d in re.findall(r"[월화수목금토일]", m.group(1))]
    weekly = sorted(set(weekly))
    if not weekly and not nth:
        return None                      # '설날·추석 당일' 같은 것만 → 요일로는 모름
    return {"always_open": False, "closed_dow": weekly, "closed_nth": nth}


def open_hours(usetime):
    text = _clean(usetime)
    if not text.strip():
        return None
    if re.search(r"24\s*시간", text):
        return {"open": 0, "close": 24 * 60}
    m = _TIME.search(text)
    if not m:
        return None
    h1, m1, h2, m2 = int(m.group(1)), int(m.group(2) or 0), int(m.group(3)), int(m.group(4) or 0)
    if not (0 <= h1 <= 24 and 0 <= h2 <= 30 and m1 < 60 and m2 < 60):
        return None
    o, c = h1 * 60 + m1, h2 * 60 + m2
    if c <= o:                           # 새벽까지 (18:00~02:00)
        c += 24 * 60
    return {"open": o, "close": c}


def parse(usetime, restdate):
    out = {}
    h = open_hours(usetime)
    if h:
        out.update(h)
    d = closed_days(restdate)
    if d:
        out.update(d)
    return out or None
