"""매일 자동 수집 — 작업 스케줄러가 하루 한 번 부른다 (scripts/daily_collect.ps1).

하는 일
  1. 지정한 지역(--areas)의 목록이 DB 에 아직 없으면 목록부터 받는다 (지역당 수십 회).
  2. 상세(detailCommon2) → 소개정보(detailIntro2) 순으로 남은 한도만큼 이어서 받는다.
     어디까지 했는지는 DB(detail_status, intro_status)가 기억하므로 매일 돌리면 알아서 이어간다.
  3. 지역별로 남은 건수를 찍고 끝난다.

한도는 TOUR_DAILY_LIMIT 를 따른다. 스케줄 실행에서는 950 으로 낮춰서 부르므로
그날 손으로 테스트할 몫(50회)이 남는다.

    docker compose run --rm -e TOUR_DAILY_LIMIT=950 ingest python -m ingest.daily --areas 6
"""
import argparse
import subprocess
import sys

from .config import TOUR_DAILY_LIMIT
from .db import connect, tour_used_today


def step(*args):
    print(f"\n$ python -m {' '.join(args)}", flush=True)
    subprocess.run([sys.executable, "-m", *args], check=False)


def status(conn):
    rows = conn.execute("""
        SELECT area_code, count(*) total,
               count(*) FILTER (WHERE detail_status = 'pending') detail_left,
               count(*) FILTER (WHERE intro_status  = 'pending') intro_left
        FROM places GROUP BY area_code ORDER BY area_code""").fetchall()
    print("\n지역별 남은 건수")
    for r in rows:
        print(f"  지역 {r['area_code']:>3} · 전체 {r['total']:>5}"
              f" · 상세 남음 {r['detail_left']:>5} · 소개 남음 {r['intro_left']:>5}")
    left = sum(r["detail_left"] + r["intro_left"] for r in rows)
    print(f"  → 남은 호출 약 {left}회, 하루 {TOUR_DAILY_LIMIT}회 기준 약 {-(-left // TOUR_DAILY_LIMIT)}일")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--areas", default="6", help="쉼표로. 6=부산 32=강원 39=제주")
    args = ap.parse_args()
    areas = [int(a) for a in args.areas.split(",") if a.strip()]

    with connect() as conn:
        print(f"오늘(KST) 사용 {tour_used_today(conn)}/{TOUR_DAILY_LIMIT}")
        need_list = [a for a in areas if conn.execute(
            "SELECT count(*) c FROM places WHERE area_code = %s", (a,)).fetchone()["c"] == 0]

    for a in need_list:
        step("ingest.fetch_list", "--area", str(a))
    step("ingest.fetch_detail", "--budget", str(TOUR_DAILY_LIMIT))
    step("ingest.fetch_intro", "--budget", str(TOUR_DAILY_LIMIT))

    with connect() as conn:
        print(f"\n오늘(KST) 사용 {tour_used_today(conn)}/{TOUR_DAILY_LIMIT}")
        status(conn)


if __name__ == "__main__":
    main()
