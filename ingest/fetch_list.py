"""목록 수집 — 유형별로 전부 받아 places 에 넣는다. overview 는 비워 둔다.

100건씩 받으므로 제주 전체(음식점 포함)도 40회 안쪽이면 끝난다.
같은 걸 다시 돌려도 안전하다(upsert). modifiedtime 이 바뀌면 갱신된다.

    docker compose run --rm ingest python -m ingest.fetch_list
    docker compose run --rm ingest python -m ingest.fetch_list --types 12,14
"""
import argparse
import time

from .config import AREA_CODE, CONTENT_TYPES
from .db import connect
from .tourapi import call, items, QuotaExceeded

LIST_OP = "areaBasedList2"

UPSERT = """
INSERT INTO places (content_id, content_type, title, addr, addr_detail,
                    area_code, sigungu_code, cat1, cat2, cat3,
                    lat, lon, image_url, thumb_url, tel,
                    priority, list_modified, ldong_regn, ldong_sgg)
VALUES (%(content_id)s, %(content_type)s, %(title)s, %(addr)s, %(addr_detail)s,
        %(area_code)s, %(sigungu_code)s, %(cat1)s, %(cat2)s, %(cat3)s,
        %(lat)s, %(lon)s, %(image_url)s, %(thumb_url)s, %(tel)s,
        %(priority)s, %(list_modified)s, %(ldong_regn)s, %(ldong_sgg)s)
ON CONFLICT (content_id) DO UPDATE SET
  title        = EXCLUDED.title,
  addr         = EXCLUDED.addr,
  addr_detail  = EXCLUDED.addr_detail,
  cat1 = EXCLUDED.cat1, cat2 = EXCLUDED.cat2, cat3 = EXCLUDED.cat3,
  lat = EXCLUDED.lat, lon = EXCLUDED.lon,
  image_url = EXCLUDED.image_url, thumb_url = EXCLUDED.thumb_url,
  tel = EXCLUDED.tel,
  priority = EXCLUDED.priority,
  ldong_regn = coalesce(EXCLUDED.ldong_regn, places.ldong_regn),
  ldong_sgg  = coalesce(EXCLUDED.ldong_sgg, places.ldong_sgg),
  -- 원본이 갱신됐으면 상세를 다시 받아야 한다
  detail_status = CASE WHEN places.list_modified IS DISTINCT FROM EXCLUDED.list_modified
                       THEN 'pending' ELSE places.detail_status END,
  list_modified = EXCLUDED.list_modified
"""


def num(v):
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def as_int(v):
    try:
        return int(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


# 법정동 시도 코드(lDongRegnCd) → TourAPI 지역 코드(areaCode).
# 축제 검색(searchFestival2)처럼 areacode 가 비어서 오는 응답이 있어 법정동 코드로 지역을 정한다.
LDONG_TO_AREA = {"11": 1, "28": 2, "30": 3, "27": 4, "29": 5, "26": 6, "31": 7, "36": 8, "41": 31,
                 "42": 32, "51": 32, "43": 33, "44": 34, "47": 35, "48": 36, "45": 37, "52": 37,
                 "46": 38, "50": 39}


def area_of(it):
    a = as_int(it.get("areacode"))
    if a:
        return a
    return LDONG_TO_AREA.get(str(it.get("lDongRegnCd") or "").strip()[:2])


def row_of(it, priority):
    return dict(
        content_id=int(it["contentid"]),
        content_type=int(it["contenttypeid"]),
        title=(it.get("title") or "").strip(),
        addr=(it.get("addr1") or "").strip() or None,
        addr_detail=(it.get("addr2") or "").strip() or None,
        area_code=area_of(it),
        sigungu_code=as_int(it.get("sigungucode")),
        cat1=it.get("cat1") or None,
        cat2=it.get("cat2") or None,
        cat3=it.get("cat3") or None,
        lat=num(it.get("mapy")),          # mapy = 위도
        lon=num(it.get("mapx")),          # mapx = 경도
        image_url=it.get("firstimage") or None,
        thumb_url=it.get("firstimage2") or None,
        tel=(it.get("tel") or "").strip() or None,
        priority=priority,
        list_modified=it.get("modifiedtime") or None,
        # 법정동 코드 — 관광지 집중률(혼잡도) API 가 이 코드로만 받는다
        ldong_regn=(str(it.get("lDongRegnCd") or "").strip() or None),
        ldong_sgg=(str(it.get("lDongSignguCd") or "").strip() or None),
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--types", default="", help="쉼표로. 비우면 전부")
    ap.add_argument("--area", type=int, default=AREA_CODE)
    ap.add_argument("--rows", type=int, default=100)
    args = ap.parse_args()

    types = [int(t) for t in args.types.split(",") if t.strip()] or list(CONTENT_TYPES)

    with connect() as conn:
        total_saved = 0
        for ctype in types:
            name, priority = CONTENT_TYPES.get(ctype, (str(ctype), 5))
            page, saved, total = 1, 0, None
            while True:
                try:
                    body = call(conn, LIST_OP, {
                        "numOfRows": args.rows, "pageNo": page,
                        "areaCode": args.area, "contentTypeId": ctype,
                        "arrange": "C",
                    })
                except QuotaExceeded as e:
                    print(f"\n중단: {e}")
                    # 0 이 아닌 종료코드 → daily.py 가 이 지역 목록을 '덜 받음' 으로 남겨 다음 날 다시 받는다
                    raise SystemExit(2)
                if total is None:
                    total = int(body.get("totalCount") or 0)
                    print(f"[{ctype} {name}] 총 {total}건")
                rows = items(body)
                if not rows:
                    break
                with conn.cursor() as cur:
                    for it in rows:
                        try:
                            cur.execute(UPSERT, row_of(it, priority))
                            saved += 1
                        except Exception as e:
                            print(f"  ! {it.get('contentid')} {it.get('title')}: {e}")
                print(f"  {saved}/{total}")
                if saved >= total or len(rows) < args.rows:
                    break
                page += 1
                time.sleep(0.2)
            total_saved += saved

        print(f"\n합계 {total_saved}건 저장")
        for r in conn.execute("SELECT * FROM ingest_progress").fetchall():
            print("  ", dict(r))


if __name__ == "__main__":
    main()
