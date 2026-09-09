"""수집된 데이터 훑어보기 — 청킹 기준과 실내 판정 규칙을 정하기 위한 근거."""
from .config import CONTENT_TYPES
from .db import connect

CAT3_HINT = """
SELECT cat3, count(*) n, min(title) sample
FROM places WHERE content_type = %s
GROUP BY cat3 ORDER BY n DESC LIMIT 25
"""


def main():
    with connect() as conn:
        print("■ overview 길이 분포")
        for r in conn.execute("""
            SELECT content_type,
                   count(*) n,
                   round(avg(length(overview)))::int avg,
                   min(length(overview)) min,
                   percentile_disc(0.5) WITHIN GROUP (ORDER BY length(overview))::int med,
                   percentile_disc(0.9) WITHIN GROUP (ORDER BY length(overview))::int p90,
                   max(length(overview)) max,
                   count(*) FILTER (WHERE length(overview) > 1000) over1000
            FROM places WHERE overview IS NOT NULL
            GROUP BY content_type ORDER BY content_type""").fetchall():
            name = CONTENT_TYPES.get(r["content_type"], ("?",))[0]
            print(f"  {name:<9} {r['n']:>4}건 · 평균 {r['avg']:>5} · 중앙 {r['med']:>5}"
                  f" · p90 {r['p90']:>5} · 최대 {r['max']:>6} · 1000자 초과 {r['over1000']:>3}")

        print("\n■ 좌표/이미지 결측")
        r = conn.execute("""SELECT count(*) n,
                              count(*) FILTER (WHERE lat IS NULL OR lon IS NULL) no_geo,
                              count(*) FILTER (WHERE image_url IS NULL OR image_url='') no_img
                            FROM places""").fetchone()
        print(f"  전체 {r['n']} · 좌표 없음 {r['no_geo']} · 대표이미지 없음 {r['no_img']}")

        for ct in (12, 14):
            name = CONTENT_TYPES.get(ct, ("?",))[0]
            print(f"\n■ {name}(cat3) 분포 — 실내 판정 규칙의 재료")
            for r in conn.execute(CAT3_HINT, (ct,)).fetchall():
                print(f"  {r['cat3']:<10} {r['n']:>4}  예) {r['sample']}")

        print("\n■ 실내로 보이는 키워드가 제목에 있는 것")
        r = conn.execute("""SELECT count(*) n FROM places
            WHERE title ~ '(박물관|미술관|전시관|아쿠아리움|수족관|과학관|기념관|실내)'""").fetchone()
        print(f"  {r['n']}건")
        for r in conn.execute("""SELECT content_type, title FROM places
            WHERE title ~ '(박물관|미술관|전시관|아쿠아리움|수족관|과학관|기념관|실내)'
            ORDER BY content_type, title LIMIT 15""").fetchall():
            print(f"    [{r['content_type']}] {r['title']}")


if __name__ == "__main__":
    main()
