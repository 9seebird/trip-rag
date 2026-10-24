"""기간 축제 수집 — searchFestival2.

목록(areaBasedList2)은 '지금 등록돼 있는 것' 만 줘서 축제가 거의 안 잡히고, 기간(언제부터 언제까지)도 없다.
축제는 기간을 지정해 따로 받아야 한다. 받은 축제는 places 에 넣고(유형 15) 기간을 event_start/end 에 적는다.
상세·소개정보는 평소 파이프라인(fetch_detail → fetch_intro → build_index)이 이어서 받는다.

- 기준일은 오늘(KST)에서 --back 일 전. 이미 시작해서 진행 중인 축제도 잡으려고 조금 앞에서부터 받는다.
- 지역마다 1~3회 정도라 하루 한도에 부담이 적다. 매일 돌려도 된다 (upsert).

    docker compose -f docker-compose.prod.yml --profile tools run --rm -T ingest \\
        python -m ingest.fetch_festival --areas 39,6,32
"""
import argparse
import time
from datetime import datetime, timedelta, timezone

from .config import CONTENT_TYPES
from .db import connect
from .fetch_list import UPSERT, row_of
from .tourapi import call, items, QuotaExceeded

OP = "searchFestival2"
KST = timezone(timedelta(hours=9))


def ymd(v):
    """'20261024' → '2026-10-24'. 이상한 값은 None."""
    v = (v or "").strip()
    if len(v) != 8 or not v.isdigit():
        return None
    return f"{v[:4]}-{v[4:6]}-{v[6:]}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--areas", required=True, help="쉼표로 (scripts/areas.txt 와 같은 코드)")
    ap.add_argument("--back", type=int, default=45, help="오늘보다 며칠 앞에서 시작한 축제부터 (진행 중인 것)")
    ap.add_argument("--rows", type=int, default=100)
    args = ap.parse_args()
    areas = list(dict.fromkeys(int(a) for a in args.areas.split(",") if a.strip()))
    since = (datetime.now(KST).date() - timedelta(days=args.back)).strftime("%Y%m%d")
    priority = CONTENT_TYPES.get(15, ("축제공연행사", 3))[1]

    with connect() as conn:
        total_saved = 0
        for area in areas:
            page, saved, total = 1, 0, None
            while True:
                try:
                    body = call(conn, OP, {"numOfRows": args.rows, "pageNo": page, "arrange": "C",
                                           "eventStartDate": since, "areaCode": area})
                except QuotaExceeded as e:
                    print(f"\n한도 도달로 중단: {e}")
                    raise SystemExit(2)
                except Exception as e:                       # 한 지역이 실패해도 다음 지역은 받는다
                    print(f"  ! 지역 {area}: {e}")
                    break
                if total is None:
                    total = int(body.get("totalCount") or 0)
                rows = items(body)
                if not rows:
                    break
                with conn.cursor() as cur:
                    for it in rows:
                        try:
                            r = row_of(it, priority)
                            r["content_type"] = 15
                            cur.execute(UPSERT, r)
                            cur.execute("UPDATE places SET event_start = %s, event_end = %s WHERE content_id = %s",
                                        (ymd(it.get("eventstartdate")), ymd(it.get("eventenddate")),
                                         r["content_id"]))
                            saved += 1
                        except Exception as e:
                            print(f"  ! {it.get('contentid')} {it.get('title')}: {e}")
                if saved >= total or len(rows) < args.rows:
                    break
                page += 1
                time.sleep(0.2)
            print(f"[지역 {area}] 축제 {saved}/{total or 0}건")
            total_saved += saved

        up = conn.execute("""SELECT count(*) c FROM places WHERE content_type = 15
                             AND event_end >= (now() AT TIME ZONE 'Asia/Seoul')::date""").fetchone()["c"]
        print(f"\n합계 {total_saved}건 저장 · 지금 진행 중이거나 앞으로 열릴 축제 {up}건")


if __name__ == "__main__":
    main()
