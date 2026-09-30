"""공개 배포용 사용량 제한.

검색·코스는 한 번 부를 때마다 비즈라우터(임베딩·리랭커·생성)에 돈이 나간다.
그래서 두 겹으로 막는다.

  1. IP 당 분당 N회   — 한 사람이 연타하거나 봇이 긁는 것을 막는다
  2. 하루 전체 N회    — 무슨 일이 있어도 하루 비용 상한을 넘지 않게 (KST 자정 리셋)

상태는 프로세스 메모리에만 둔다. 워커 1개로 도는 작은 서비스라 이걸로 충분하고,
재시작하면 초기화되는 건 감수한다. nginx 의 limit_req 가 앞단에서 한 번 더 막는다.
"""
import os
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, Request

KST = timezone(timedelta(hours=9))
PER_MIN = int(os.getenv("RATE_PER_MIN", "12"))          # IP 당 분당
DAILY = int(os.getenv("DAILY_REQUEST_LIMIT", "1500"))   # 하루 전체 (검색+코스)

_hits: dict[str, deque] = defaultdict(deque)
_day = {"date": None, "count": 0}


def client_ip(req: Request) -> str:
    # nginx 가 X-Real-IP 를 넘겨준다. 직접 붙으면 소켓 주소.
    return req.headers.get("x-real-ip") or (req.client.host if req.client else "?")


def check(req: Request):
    today = datetime.now(KST).date()
    if _day["date"] != today:              # 날짜가 바뀌면 전체 카운트와 IP 기록을 비운다
        _day.update(date=today, count=0)
        _hits.clear()

    now = time.time()
    q = _hits[client_ip(req)]
    while q and now - q[0] > 60:
        q.popleft()
    if len(q) >= PER_MIN:
        raise HTTPException(429, "요청이 너무 잦습니다. 잠시 후 다시 시도해 주세요.")
    if _day["count"] >= DAILY:
        raise HTTPException(503, "오늘 준비한 검색량을 모두 썼습니다. 내일 다시 이용해 주세요.")

    q.append(now)
    _day["count"] += 1


def usage():
    return {"today": _day["count"], "daily_limit": DAILY, "per_min": PER_MIN}
