"""기간 축제 수집 — searchFestival2.

목록(areaBasedList2)은 '지금 등록돼 있는 것' 만 줘서 축제가 거의 안 잡히고, 기간(언제부터 언제까지)도 없다.
축제는 기간을 지정해 따로 받아야 한다. 받은 축제는 places 에 넣고(유형 15) 기간을 event_start/end 에 적는다.
상세·소개정보는 평소 파이프라인(fetch_detail → fetch_intro → build_index)이 이어서 받는다.

- 기준일은 오늘(KST)에서 --back 일 전. 이미 시작해서 진행 중인 축제도 잡으려고 조금 앞에서부터 받는다.
- 전국을 한 번에 받고(지역 코드로 거르면 0건이 오던 문제) 여기서 대상 지역만 남긴다. 몇 회 호출이면 끝난다 (upsert).
- 조건별로 몇 건 나오는지만 보려면 --probe

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


def probe(conn, since):
    """어떤 조건에서 축제가 나오는지 확인 (호출 몇 번만 쓴다)."""
    today = datetime.now(KST).date().strftime("%Y%m%d")
    cases = [("전국 · 시작일 " + since, {"eventStartDate": since}),
             ("전국 · 시작일 오늘", {"eventStartDate": today}),
             ("areaCode=39(제주)", {"eventStartDate": since, "areaCode": 39}),
             ("lDongRegnCd=50(제주)", {"eventStartDate": since, "lDongRegnCd": 50})]
    for name, extra in cases:
        try:
            body = call(conn, OP, {"numOfRows": 3, "pageNo": 1, "arrange": "C", **extra})
            rows = items(body)
            first = rows[0] if rows else {}
            print(f"{name:28} → totalCount {body.get('totalCount')} · 첫 건: {first.get('title')} "
                  f"({first.get('eventstartdate')}~{first.get('eventenddate')}, areacode={first.get('areacode')}, lDongRegnCd={first.get('lDongRegnCd')})")
        except Exception as e:
            print(f"{name:28} → 오류 {e}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--areas", required=True, help="쉼표로 (scripts/areas.txt 와 같은 코드)")
    ap.add_argument("--back", type=int, default=45, help="오늘보다 며칠 앞에서 시작한 축제부터 (진행 중인 것)")
    ap.add_argument("--rows", type=int, default=100)
    ap.add_argument("--probe", action="store_true", help="조건별로 몇 건 나오는지만 확인")
    args = ap.parse_args()
    areas = set(int(a) for a in args.areas.split(",") if a.strip())
    since = (datetime.now(KST).date() - timedelta(days=args.back)).strftime("%Y%m%d")
    priority = CONTENT_TYPES.get(15, ("축제공연행사", 3))[1]

    with connect() as conn:
        if args.probe:
            probe(conn, since)
            return
        # 지역 코드(areaCode)로 거르면 0건이 오고 응답의 areacode 도 비어 있다(법정동 코드 체계로 바뀜).
        # 전국을 한 번에 받고, 법정동 시도 코드(lDongRegnCd)로 지역을 정해 고른다 (fetch_list.area_of).
        # 전국 축제는 수백 건이라 100건씩 몇 페이지면 끝난다.
        page, saved, skipped, total, by = 1, 0, 0, None, {}
        while True:
            try:
                body = call(conn, OP, {"numOfRows": args.rows, "pageNo": page, "arrange": "C",
                                       "eventStartDate": since})
            except QuotaExceeded as e:
                print(f"\n한도 도달로 중단: {e}")
                raise SystemExit(2)
            except Exception as e:
                print(f"  ! {e}")
                break
            if total is None:
                total = int(body.get("totalCount") or 0)
                print(f"전국 축제 {total}건 (시작일 {since} 이후)")
            rows = items(body)
            if not rows:
                break
            with conn.cursor() as cur:
                for it in rows:
                    try:
                        r = row_of(it, priority)
                        if r["area_code"] not in areas:
                            skipped += 1
                            continue
                        r["content_type"] = 15
                        cur.execute(UPSERT, r)
                        cur.execute("UPDATE places SET event_start = %s, event_end = %s WHERE content_id = %s",
                                    (ymd(it.get("eventstartdate")), ymd(it.get("eventenddate")), r["content_id"]))
                        saved += 1
                        by[r["area_code"]] = by.get(r["area_code"], 0) + 1
                    except Exception as e:
                        print(f"  ! {it.get('contentid')} {it.get('title')}: {e}")
            if page * args.rows >= (total or 0) or len(rows) < args.rows:
                break
            page += 1
            time.sleep(0.2)
        print("지역별: " + ", ".join(f"{a}:{n}" for a, n in sorted(by.items())))
        up = conn.execute("""SELECT count(*) c FROM places WHERE content_type = 15
                             AND event_end >= (now() AT TIME ZONE 'Asia/Seoul')::date""").fetchone()["c"]
        print(f"\n저장 {saved}건 (대상 지역 밖 {skipped}건) · 지금 진행 중이거나 앞으로 열릴 축제 {up}건")


if __name__ == "__main__":
    main()
