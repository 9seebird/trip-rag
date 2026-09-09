"""API 스펙 확인 — 호출 3회만 쓴다.

TourAPI 는 버전에 따라 오퍼레이션 이름과 필드가 다르다(KorService1 / KorService2 …).
2,000건을 긁기 전에 '내 키로 실제로 뭐가 오는지' 를 눈으로 먼저 본다.

    docker compose run --rm ingest python -m ingest.probe
"""
import json
import sys

from .config import AREA_CODE, TOUR_BASE
from .db import connect, tour_used_today
from .tourapi import call, items, TourApiError, QuotaExceeded

LIST_OPS   = ["areaBasedList2", "areaBasedList1", "areaBasedList"]
DETAIL_OPS = ["detailCommon2", "detailCommon1", "detailCommon"]
INTRO_OPS  = ["detailIntro2", "detailIntro1", "detailIntro"]


def try_ops(conn, ops, params, label):
    for op in ops:
        try:
            body = call(conn, op, params)
            print(f"  ✓ {op}")
            return op, body
        except QuotaExceeded:
            raise
        except TourApiError as e:
            print(f"  ✗ {op} → {e}")
    print(f"  {label} 오퍼레이션을 못 찾았습니다. .env 의 TOUR_BASE 를 확인하세요.")
    return None, None


def main():
    with connect() as conn:
        print(f"BASE = {TOUR_BASE}")
        print(f"오늘(KST) 사용량 = {tour_used_today(conn)}\n")

        print("1) 목록 오퍼레이션")
        op, body = try_ops(conn, LIST_OPS,
                           {"numOfRows": 1, "pageNo": 1, "areaCode": AREA_CODE,
                            "contentTypeId": 12, "arrange": "C"},
                           "목록")
        if not body:
            sys.exit(1)
        rows = items(body)
        print(f"  제주 관광지 총 건수 totalCount = {body.get('totalCount')}")
        if not rows:
            print("  결과 0건. areaCode/contentTypeId 확인 필요")
            sys.exit(1)
        print("  필드:", ", ".join(sorted(rows[0].keys())))
        print("  샘플:", json.dumps(rows[0], ensure_ascii=False)[:400], "\n")

        cid = rows[0]["contentid"]

        print(f"2) 상세(공통) 오퍼레이션 — contentid={cid}")
        # detailCommon2 는 contentTypeId 를 받지 않는다
        op, body = try_ops(conn, DETAIL_OPS, {"contentId": cid}, "상세")
        if body:
            d = items(body)
            if d:
                print("  필드:", ", ".join(sorted(d[0].keys())))
                from .textclean import clean as _c
                ov = _c(d[0].get("overview")) or ""
                print(f"  overview 길이 = {len(ov)}자")
                print("  overview 앞부분:", ov[:200].replace("\n", " "), "\n")
                if not ov:
                    print("  ⚠ overview 가 비었습니다.\n")

        print(f"3) 상세(소개) 오퍼레이션 — contentid={cid}")
        op, body = try_ops(conn, INTRO_OPS, {"contentId": cid, "contentTypeId": 12}, "소개")
        if body:
            d = items(body)
            if d:
                print("  필드:", ", ".join(sorted(d[0].keys())))

        print(f"\n총 사용량(KST 오늘) = {tour_used_today(conn)}")


if __name__ == "__main__":
    main()
