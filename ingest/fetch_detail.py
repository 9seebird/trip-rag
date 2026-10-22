"""상세 수집 — overview 를 채운다. 하루 한도를 쓰고 멈추고, 다음에 이어서 한다.

이 스크립트의 요점
  * 어디까지 했는지는 DB(places.detail_status)가 기억한다. 중간에 끊겨도 다시 돌리면 이어서 간다.
  * priority 순으로 간다. 관광지·문화시설 → 레포츠 → 축제 → 음식점.
    음식점은 코스 짜기에 이름·좌표·분류만 있으면 되므로 소개글은 뒤로 미룬다.
  * 남은 한도를 KST 자정 기준으로 세고, 예산만큼만 쓰고 멈춘다.
  * 실패한 건은 failed 로 두고 --retry-failed 로 따로 재시도한다.

    docker compose run --rm ingest python -m ingest.fetch_detail --budget 900
    docker compose run --rm ingest python -m ingest.fetch_detail --budget 900 --loop
"""
import argparse
import hashlib
import time
from datetime import datetime

from .config import TOUR_DAILY_LIMIT, CONTENT_TYPES
from .db import connect, tour_used_today
from .textclean import clean, first_url
from .tourapi import call, items, QuotaExceeded, TourApiError

DETAIL_OP = "detailCommon2"

PICK = """
SELECT content_id, content_type, title
FROM places
WHERE detail_status = %s
  AND (%s = 0 OR content_type = %s)
ORDER BY priority, array_position(%s::int[], area_code) NULLS LAST, content_id
LIMIT %s
"""


def sha(s: str) -> str:
    return hashlib.sha256((s or "").encode("utf-8")).hexdigest()[:16]


def save(conn, cid, d):
    ov = clean(d.get("overview")) or ""
    conn.execute(
        """UPDATE places SET
             overview = %s, overview_hash = %s,
             homepage = COALESCE(NULLIF(%s,''), homepage),
             tel      = COALESCE(NULLIF(%s,''), tel),
             image_url= COALESCE(image_url, NULLIF(%s,'')),
             detail_status = 'done', detail_error = NULL,
             detail_tries = detail_tries + 1,
             fetched_at = now()
           WHERE content_id = %s""",
        (ov or None, sha(ov) if ov else None,
         first_url(d.get("homepage")) or "",
         clean(d.get("tel")) or "",
         (d.get("firstimage") or "").strip(),
         cid),
    )


def fail(conn, cid, msg):
    conn.execute(
        """UPDATE places SET detail_status = 'failed', detail_error = %s,
             detail_tries = detail_tries + 1 WHERE content_id = %s""",
        (str(msg)[:300], cid),
    )


def run_once(conn, budget, status, only_type, sleep, areas=()):
    used = tour_used_today(conn)
    room = max(0, TOUR_DAILY_LIMIT - used)
    n = min(budget, room)
    if n <= 0:
        print(f"오늘 한도 소진 (사용 {used}/{TOUR_DAILY_LIMIT}). 자정(KST) 이후 다시.")
        return 0, True

    rows = conn.execute(PICK, (status, 0 if not only_type else 1, only_type or 0, list(areas), n)).fetchall()
    if not rows:
        print(f"'{status}' 상태인 대상이 없습니다.")
        return 0, True

    print(f"오늘 사용 {used}/{TOUR_DAILY_LIMIT} · 이번에 {len(rows)}건 시도")
    done = 0
    for i, r in enumerate(rows, 1):
        cid = r["content_id"]
        try:
            # detailCommon2 는 contentTypeId 를 보내면 INVALID_REQUEST_PARAMETER_ERROR 를 낸다.
            # contentId 하나만 보내면 overview 를 포함해 전부 돌려준다.
            body = call(conn, DETAIL_OP, {"contentId": cid})
            d = items(body)
            if d:
                save(conn, cid, d[0])
                done += 1
                ov = len((d[0].get("overview") or ""))
                mark = "·" if ov else "○"      # ○ = 소개글이 원래 없는 장소
            else:
                fail(conn, cid, "empty items")
                mark = "×"
        except QuotaExceeded as e:
            print(f"\n한도 도달로 중단: {e}")
            return done, True
        except TourApiError as e:
            fail(conn, cid, e)
            mark = "×"
        if i % 50 == 0 or i == len(rows):
            print(f"  {i}/{len(rows)} {mark} {r['title'][:20]}")
        time.sleep(sleep)
    return done, False


def report(conn):
    print("\n진행 현황")
    for r in conn.execute("SELECT * FROM ingest_progress").fetchall():
        name = CONTENT_TYPES.get(r["content_type"], ("?",))[0]
        print(f"  {r['content_type']:>3} {name:<9} 전체 {r['total']:>5}"
              f" · 완료 {r['detail_done']:>5} · 남음 {r['detail_pending']:>5}"
              f" · 실패 {r['detail_failed']:>4} · 소개글 있음 {r['has_overview']:>5}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--budget", type=int, default=900, help="이번 실행에서 쓸 최대 호출 수")
    ap.add_argument("--type", type=int, default=0, help="특정 contentTypeId 만")
    ap.add_argument("--retry-failed", action="store_true")
    ap.add_argument("--sleep", type=float, default=0.12)
    ap.add_argument("--areas", default="",
                    help="쉼표로. 같은 우선순위 안에서 이 지역 순서대로 (목록에 없는 지역은 맨 뒤)")
    ap.add_argument("--loop", action="store_true",
                    help="한도 소진 후 잠들었다가 KST 자정 지나면 자동으로 이어서 (밤새 걸어둘 때)")
    args = ap.parse_args()

    status = "failed" if args.retry_failed else "pending"
    areas = [int(a) for a in args.areas.split(",") if a.strip()]

    with connect() as conn:
        while True:
            done, stop = run_once(conn, args.budget, status, args.type, args.sleep, areas)
            print(f"\n{done}건 저장")
            report(conn)
            if not args.loop:
                return
            left = conn.execute(
                "SELECT count(*) c FROM places WHERE detail_status = %s", (status,)
            ).fetchone()["c"]
            if left == 0:
                print("\n남은 게 없습니다. 종료.")
                return
            print(f"\n남은 {left}건. 한도 초기화(KST 자정)까지 대기…"
                  f" 지금 {datetime.now():%Y-%m-%d %H:%M}")
            time.sleep(30 * 60)


if __name__ == "__main__":
    main()
