"""부가 정보 수집 — 반려동물 동반 · 무장애 · 추가 사진.

    python -m ingest.fetch_extra pet    --areas 39,6,32     # 반려동물 동반여행 (KorPetTourService2)
    python -m ingest.fetch_extra access --areas 39,6,32     # 무장애 여행 (KorWithService2)
    python -m ingest.fetch_extra images --areas 39,6,32     # 대표 사진이 없는 곳 (KorService2 detailImage2)

pet · access
  1) 지역별 목록(areaBasedList2)으로 '그 서비스에 등록된 곳' 을 표시한다 (pet_allowed / access_listed = TRUE).
     목록은 잘 안 바뀌어서 지역마다 7일에 한 번만 다시 받는다 (extra_lists).
  2) 표시된 곳 중 상세가 없는 곳을 상세(detailPetTour2 / detailWithTour2)로 채운다. 하루 예산만큼.
  → 공공데이터포털에서 서비스마다 **활용신청을 따로** 해야 한다. 신청 전이면 조용히 건너뛴다(종료코드 0).
  → 한도도 서비스마다 따로라 api 이름(pet / access)으로 따로 센다 (PET_DAILY_LIMIT, ACCESS_DAILY_LIMIT).

images
  관광지·문화시설·레포츠 → 음식점 순으로, 사진이 없는 곳의 사진 목록 첫 장을 대표 사진으로 쓴다.
  KorService2 라 TourAPI 하루 한도를 같이 쓴다 → 상세·소개정보를 다 받고 남은 몫만 쓴다 (daily.py 가 마지막에 부른다).
"""
import argparse
import json
import os
import re
import time

from .config import TOUR_DAILY_LIMIT
from .db import connect, tour_used_today, used_today
from .tourapi import call, items, QuotaExceeded, TourApiError

SERVICES = {
    "pet":    dict(service="KorPetTourService2", list_op="areaBasedList2", detail_op="detailPetTour2",
                   flag="pet_allowed", info="pet_info",
                   limit=int(os.getenv("PET_DAILY_LIMIT", "950"))),
    "access": dict(service="KorWithService2", list_op="areaBasedList2", detail_op="detailWithTour2",
                   flag="access_listed", info="access_info",
                   limit=int(os.getenv("ACCESS_DAILY_LIMIT", "950"))),
}
NOT_REGISTERED = ("30", "SERVICE_KEY_IS_NOT_REGISTERED_ERROR", "20", "SERVICE_ACCESS_DENIED_ERROR")
NO = re.compile(r"(없음|불가|미설치|미보유|^\s*-\s*$)")


def clean(d):
    """빈 값·공통 필드를 뺀 상세 원본."""
    skip = {"contentid", "contenttypeid"}
    return {k: v for k, v in (d or {}).items() if k not in skip and v not in (None, "", "0")}


def has(v):
    return bool(v) and not NO.search(str(v))


def run_lists(conn, name, cfg, areas):
    marked = 0
    for area in areas:
        fresh = conn.execute("""SELECT 1 FROM extra_lists WHERE service = %s AND area_code = %s
                                AND done_at > now() - interval '7 days'""", (name, area)).fetchone()
        if fresh:
            continue
        page, ids, total = 1, [], None
        while True:
            body = call(conn, cfg["list_op"], {"numOfRows": 100, "pageNo": page, "arrange": "C", "areaCode": area},
                        service=cfg["service"], api=name, limit=cfg["limit"])
            if total is None:
                total = int(body.get("totalCount") or 0)
            rows = items(body)
            ids += [int(r["contentid"]) for r in rows if str(r.get("contentid", "")).isdigit()]
            if not rows or len(ids) >= total or len(rows) < 100:
                break
            page += 1
            time.sleep(0.2)
        if ids:
            n = conn.execute(f"UPDATE places SET {cfg['flag']} = TRUE WHERE content_id = ANY(%s)", (ids,)).rowcount
            marked += n
        conn.execute("""INSERT INTO extra_lists (service, area_code) VALUES (%s, %s)
                        ON CONFLICT (service, area_code) DO UPDATE SET done_at = now()""", (name, area))
        print(f"  [{name} 지역 {area}] 목록 {len(ids)}곳 · 우리 DB 와 겹침 {n if ids else 0}곳")
    return marked


def run_details(conn, name, cfg, areas, budget):
    rows = conn.execute(f"""SELECT content_id, content_type FROM places
                            WHERE {cfg['flag']} AND {cfg['info']} IS NULL AND area_code = ANY(%s)
                            ORDER BY priority, content_id LIMIT %s""", (areas, budget)).fetchall()
    done = 0
    for r in rows:
        try:
            body = call(conn, cfg["detail_op"], {"contentId": r["content_id"]},
                        service=cfg["service"], api=name, limit=cfg["limit"])
        except TourApiError as e:
            print(f"  ! {r['content_id']}: {e}")
            continue
        got = items(body)
        info = clean(got[0]) if got else {}
        params = [json.dumps(info, ensure_ascii=False)]
        extra = ""
        if name == "access":
            # 유모차: 대여·이동 가능 안내가 있으면, 휠체어: 대여·경사로 등 접근 안내가 있으면
            extra = ", stroller_ok = %s, wheelchair_ok = %s"
            params += [has(info.get("stroller")) or None, has(info.get("wheelchair")) or None]
        conn.execute(f"UPDATE places SET {cfg['info']} = %s::jsonb{extra} WHERE content_id = %s",
                     (*params, r["content_id"]))
        done += 1
        time.sleep(0.1)
    return done, len(rows)


def run_service(name, areas, budget):
    cfg = SERVICES[name]
    with connect() as conn:
        try:
            marked = run_lists(conn, name, cfg, areas)
            left = max(0, cfg["limit"] - used_today(conn, name))
            done, want = run_details(conn, name, cfg, areas, min(budget, left))
        except QuotaExceeded as e:
            print(f"한도 도달로 중단: {e}")
            return
        except TourApiError as e:
            if e.code in NOT_REGISTERED or "NOT_REGISTERED" in str(e) or "ACCESS_DENIED" in str(e):
                print(f"{cfg['service']} 활용신청이 안 돼 있어 건너뜀 (공공데이터포털에서 신청) — {e}")
                return
            raise
        n = conn.execute(f"SELECT count(*) c FROM places WHERE {cfg['flag']}").fetchone()["c"]
        print(f"{name}: 이번에 표시 {marked}곳 · 상세 {done}/{want} · 전체 표시 {n}곳")


def run_images(areas, budget):
    with connect() as conn:
        left = max(0, TOUR_DAILY_LIMIT - tour_used_today(conn))
        n = min(budget, left)
        if n <= 0:
            print("사진: 오늘 TourAPI 한도가 남지 않아 건너뜀")
            return
        rows = conn.execute("""SELECT content_id, title FROM places
                               WHERE image_url IS NULL AND image_status IS NULL AND detail_status = 'done'
                                 AND area_code = ANY(%s)
                               ORDER BY priority, content_id LIMIT %s""", (areas, n)).fetchall()
        found = 0
        params_ok = {"imageYN": "Y"}
        for r in rows:
            try:
                body = call(conn, "detailImage2", {"contentId": r["content_id"], **params_ok})
            except QuotaExceeded as e:
                print(f"한도 도달로 중단: {e}")
                break
            except TourApiError as e:
                if params_ok and "PARAMETER" in str(e).upper():      # 옛 플래그를 거부하면 빼고 다시
                    params_ok = {}
                    try:
                        body = call(conn, "detailImage2", {"contentId": r["content_id"]})
                    except TourApiError as e2:
                        print(f"  ! {r['content_id']}: {e2}"); continue
                else:
                    print(f"  ! {r['content_id']}: {e}"); continue
            imgs = [i for i in items(body) if i.get("originimgurl")]
            if imgs:
                conn.execute("""UPDATE places SET image_url = %s, thumb_url = %s, image_status = 'done'
                                WHERE content_id = %s""",
                             (imgs[0]["originimgurl"], imgs[0].get("smallimageurl"), r["content_id"]))
                found += 1
            else:
                conn.execute("UPDATE places SET image_status = 'none' WHERE content_id = %s", (r["content_id"],))
            time.sleep(0.1)
        print(f"사진: {len(rows)}곳 확인 · 새로 찾은 사진 {found}곳")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("what", choices=["pet", "access", "images"])
    ap.add_argument("--areas", required=True)
    ap.add_argument("--budget", type=int, default=950, help="이번에 쓸 상세·사진 호출 수 상한")
    args = ap.parse_args()
    areas = list(dict.fromkeys(int(a) for a in args.areas.split(",") if a.strip()))
    if args.what == "images":
        run_images(areas, args.budget)
    else:
        run_service(args.what, areas, args.budget)


if __name__ == "__main__":
    main()
