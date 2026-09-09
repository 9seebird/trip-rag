"""건수만 세어 본다 — 얼마나 큰 일인지 먼저 알고 시작하려고.

목록 조회는 numOfRows=1 로 불러도 totalCount 를 주므로, 호출 1회에 건수 하나를 안다.
  전국 유형별      → 5회
  지역별까지 (--by-area) → 유형 5 × 지역 17 = 85회

    docker compose run --rm ingest python -m ingest.counts
    docker compose run --rm ingest python -m ingest.counts --by-area
"""
import argparse
import time

from .config import CONTENT_TYPES, TOUR_DAILY_LIMIT
from .db import connect, tour_used_today
from .tourapi import call, QuotaExceeded

LIST_OP = "areaBasedList2"

AREAS = {
    1: "서울", 2: "인천", 3: "대전", 4: "대구", 5: "광주", 6: "부산", 7: "울산",
    8: "세종", 31: "경기", 32: "강원", 33: "충북", 34: "충남", 35: "경북",
    36: "경남", 37: "전북", 38: "전남", 39: "제주",
}


def count(conn, ctype, area=None):
    p = {"numOfRows": 1, "pageNo": 1, "contentTypeId": ctype, "arrange": "C"}
    if area:
        p["areaCode"] = area
    body = call(conn, LIST_OP, p)
    return int(body.get("totalCount") or 0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--by-area", action="store_true", help="지역별로도 센다 (85회 소모)")
    args = ap.parse_args()

    with connect() as conn:
        print(f"오늘(KST) 사용 {tour_used_today(conn)}/{TOUR_DAILY_LIMIT}\n")
        try:
            if not args.by_area:
                print(f"{'유형':<12}{'전국 건수':>10}")
                print("-" * 24)
                total = 0
                for ctype, (name, _) in CONTENT_TYPES.items():
                    n = count(conn, ctype)
                    total += n
                    print(f"{name:<12}{n:>10,}")
                    time.sleep(0.2)
                print("-" * 24)
                print(f"{'합계':<12}{total:>10,}")
                print(f"\n일 1,000회 기준 상세 수집에 약 {total/1000:.0f}일")
                print(f"음식점 제외 시 상세 수집 대상만 다시 계산하려면 위 표에서 빼서 보세요.")
            else:
                header = f"{'지역':<6}" + "".join(f"{n[0]:>9}" for n in CONTENT_TYPES.values()) + f"{'합계':>10}"
                print(header)
                print("-" * len(header))
                grand = 0
                for acode, aname in AREAS.items():
                    row, s = [], 0
                    for ctype in CONTENT_TYPES:
                        n = count(conn, ctype, acode)
                        row.append(n)
                        s += n
                        time.sleep(0.2)
                    grand += s
                    print(f"{aname:<6}" + "".join(f"{v:>9,}" for v in row) + f"{s:>10,}")
                print("-" * len(header))
                print(f"{'전국':<6}" + " " * (9 * len(CONTENT_TYPES)) + f"{grand:>10,}")
        except QuotaExceeded as e:
            print(f"\n중단: {e}")

        print(f"\n사용 후 {tour_used_today(conn)}/{TOUR_DAILY_LIMIT}")


if __name__ == "__main__":
    main()
