"""소개정보 수집 (detailIntro2) — 운영시간·휴무일·주차·애완동물·유모차 같은 구조화된 값.

overview 가 '의미 검색'의 재료라면 이쪽은 '조건 필터'의 재료다.
"애기랑", "강아지랑", "월요일에" 같은 조건을 소개글 해석 없이 정확히 거를 수 있다.

유형마다 필드 이름이 다르다 (usetime / usetimeculture / opentimefood …).
원본은 intro_raw(JSONB)에 통째로 넣고, 공통으로 쓸 것만 컬럼으로 뽑는다.

    docker compose run --rm ingest python -m ingest.migrate      # 먼저 한 번
    docker compose run --rm ingest python -m ingest.fetch_intro --budget 900
"""
import argparse
import json
import time

from .config import TOUR_DAILY_LIMIT, CONTENT_TYPES
from .db import connect, tour_used_today
from .textclean import clean
from .tourapi import call, items, QuotaExceeded, TourApiError

INTRO_OP = "detailIntro2"

# contentTypeId → {공통이름: 실제 필드명}
FIELDS = {
    12: dict(usetime="usetime", restdate="restdate", parking="parking",
             pet="chkpet", baby="chkbabycarriage", infocenter="infocenter"),
    14: dict(usetime="usetimeculture", restdate="restdateculture", parking="parkingculture",
             pet="chkpetculture", baby="chkbabycarriageculture",
             usefee="usefee", infocenter="infocenterculture"),
    15: dict(usetime="playtime", parking="parkinginfo",
             usefee="usetimefestival", infocenter="sponsor1tel"),
    28: dict(usetime="usetimeleports", restdate="restdateleports", parking="parkingleports",
             pet="chkpetleports", baby="chkbabycarriageleports",
             usefee="usefeeleports", infocenter="infocenterleports"),
    39: dict(usetime="opentimefood", restdate="restdatefood", parking="parkingfood",
             baby="kidsfacility", menu="firstmenu", infocenter="infocenterfood"),
}

PICK = """
SELECT content_id, content_type, title
FROM places
WHERE intro_status = %s AND (%s = 0 OR content_type = %s)
ORDER BY priority, content_id
LIMIT %s
"""


def yn(v, absent_is_false=True):
    """'가능' / '불가' / '없음' / '1' / '0' 같은 값을 참·거짓으로. 애매하면 None.

    absent_is_false=False 는 '없음' 을 거짓이 아니라 모름으로 본다.
    유모차 대여가 '없음' 이라고 아이랑 못 가는 곳은 아니다. 이걸 거짓으로 넣으면
    '아이랑' 조건이 유모차 대여 안 하는 곳을 전부 빼 버린다 (search._where 는 거짓만 뺀다).
    음식점 kidsfacility 는 '0'/'1' 로 온다.
    """
    if v is None:
        return None
    s = str(v).replace(" ", "")
    if not s:
        return None
    if s in ("1", "Y", "y"):
        return True
    if s in ("0", "N", "n"):
        return False if absent_is_false else None
    if any(k in s for k in ("불가", "안됨", "미가능", "불허", "금지")):
        return False
    if "없음" in s:
        return False if absent_is_false else None
    if any(k in s for k in ("가능", "있음", "동반", "허용", "무료", "대여")):
        return True
    return None


def save(conn, cid, ctype, d):
    m = FIELDS.get(ctype, {})
    g = lambda k: clean(d.get(m[k])) if m.get(k) else None
    conn.execute(
        """UPDATE places SET
             intro_raw = %s,
             usetime = COALESCE(%s, usetime),
             restdate = COALESCE(%s, restdate),
             parking = COALESCE(%s, parking),
             usefee = COALESCE(%s, usefee),
             infocenter = COALESCE(%s, infocenter),
             menu = COALESCE(%s, menu),
             pet_allowed = COALESCE(%s, pet_allowed),
             baby_ok = COALESCE(%s, baby_ok),
             intro_status = 'done', intro_tries = intro_tries + 1
           WHERE content_id = %s""",
        (json.dumps({k: v for k, v in d.items() if v not in (None, "")}, ensure_ascii=False),
         g("usetime"), g("restdate"), g("parking"), g("usefee"), g("infocenter"), g("menu"),
         yn(d.get(m.get("pet", ""))), yn(d.get(m.get("baby", "")), absent_is_false=False),
         cid),
    )


def report(conn):
    print("\n소개정보 진행")
    rows = conn.execute("""
        SELECT content_type, count(*) total,
               count(*) FILTER (WHERE intro_status='done') done,
               count(*) FILTER (WHERE intro_status='pending') pending,
               count(*) FILTER (WHERE intro_status='failed') failed,
               count(*) FILTER (WHERE pet_allowed) pet,
               count(*) FILTER (WHERE baby_ok) baby
        FROM places GROUP BY content_type ORDER BY content_type""").fetchall()
    for r in rows:
        name = CONTENT_TYPES.get(r["content_type"], ("?",))[0]
        print(f"  {r['content_type']:>3} {name:<9} 전체 {r['total']:>4} · 완료 {r['done']:>4}"
              f" · 남음 {r['pending']:>4} · 실패 {r['failed']:>3}"
              f" · 애완가능 {r['pet']:>3} · 유아시설 {r['baby']:>3}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--budget", type=int, default=900)
    ap.add_argument("--type", type=int, default=0)
    ap.add_argument("--retry-failed", action="store_true")
    ap.add_argument("--sleep", type=float, default=0.12)
    args = ap.parse_args()
    status = "failed" if args.retry_failed else "pending"

    with connect() as conn:
        used = tour_used_today(conn)
        n = min(args.budget, max(0, TOUR_DAILY_LIMIT - used))
        if n <= 0:
            print(f"오늘 한도 소진 ({used}/{TOUR_DAILY_LIMIT})")
            return
        rows = conn.execute(PICK, (status, 0 if not args.type else 1, args.type or 0, n)).fetchall()
        if not rows:
            print(f"'{status}' 상태 대상 없음")
            report(conn)
            return
        print(f"오늘 사용 {used}/{TOUR_DAILY_LIMIT} · {len(rows)}건 시도")
        done = 0
        for i, r in enumerate(rows, 1):
            cid = r["content_id"]
            try:
                body = call(conn, INTRO_OP, {"contentId": cid, "contentTypeId": r["content_type"]})
                d = items(body)
                if d:
                    save(conn, cid, r["content_type"], d[0])
                    done += 1
                else:
                    print(f"  빈 응답 {cid} {r['title']}")
                    conn.execute("UPDATE places SET intro_status='failed',"
                                 " intro_tries=intro_tries+1 WHERE content_id=%s", (cid,))
            except QuotaExceeded as e:
                print(f"\n한도 도달로 중단: {e}")
                break
            except TourApiError as e:
                print(f"  실패 {cid} {r['title']} → {e}")
                conn.execute("UPDATE places SET intro_status='failed',"
                             " intro_tries=intro_tries+1 WHERE content_id=%s", (cid,))
            if i % 100 == 0 or i == len(rows):
                print(f"  {i}/{len(rows)}")
            time.sleep(args.sleep)
        print(f"\n{done}건 저장")
        report(conn)


if __name__ == "__main__":
    main()
