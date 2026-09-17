"""소개정보(detailIntro2) 수집 결과 점검.

fetch_intro 의 요약표는 '참' 개수만 센다. 0 이 나왔을 때
(a) 원본에 값이 없어서인지 (b) 필드 이름을 잘못 짚었는지 (c) '가능/불가' 해석 규칙이 틀렸는지
를 가리려면 원본(intro_raw)을 봐야 한다.

    docker compose run --rm ingest python -m ingest.intro_check
"""
from collections import Counter

from .db import connect
from .fetch_intro import FIELDS


def main():
    with connect() as conn:
        print("① 참/거짓/모름 분포")
        for r in conn.execute("""
            SELECT content_type,
              count(*) FILTER (WHERE pet_allowed)            pet_t,
              count(*) FILTER (WHERE pet_allowed IS FALSE)   pet_f,
              count(*) FILTER (WHERE pet_allowed IS NULL)    pet_n,
              count(*) FILTER (WHERE baby_ok)                baby_t,
              count(*) FILTER (WHERE baby_ok IS FALSE)       baby_f,
              count(*) FILTER (WHERE baby_ok IS NULL)        baby_n
            FROM places GROUP BY 1 ORDER BY 1""").fetchall():
            print(f"  {r['content_type']:>3}  애완 참{r['pet_t']:>4} 거짓{r['pet_f']:>4} 모름{r['pet_n']:>4}"
                  f"   아이 참{r['baby_t']:>4} 거짓{r['baby_f']:>4} 모름{r['baby_n']:>4}")

        rows = conn.execute("SELECT content_type, intro_raw FROM places WHERE intro_raw IS NOT NULL").fetchall()
        print("\n② 원본 값 상위 (유형별 · 필드별)")
        for ct, m in FIELDS.items():
            raws = [r["intro_raw"] for r in rows if r["content_type"] == ct]
            if not raws:
                continue
            keys = Counter(k for d in raws for k in d)
            print(f"\n  [{ct}] {len(raws)}건 · 원본에 실제로 온 키: {', '.join(sorted(keys))}")
            for name in ("pet", "baby"):
                f = m.get(name)
                if not f:
                    print(f"    {name}: 매핑 없음"); continue
                vals = Counter((d.get(f) or "∅")[:30] for d in raws)
                print(f"    {name} ← {f} (값 있는 곳 {len(raws) - vals.get('∅', 0)}건): "
                      + " | ".join(f"{v!r}×{c}" for v, c in vals.most_common(8)))

        print("\n③ 실패 목록")
        for r in conn.execute("""SELECT content_id, content_type, title, intro_tries
                                 FROM places WHERE intro_status='failed' ORDER BY content_type, content_id"""):
            print(f"  {r['content_type']:>3} {r['content_id']:>8} {r['title'][:24]} (시도 {r['intro_tries']})")

        print("\n④ detailIntro2 실패 사유 (api_calls 로그)")
        rows = conn.execute("""SELECT note, count(*) n FROM api_calls
                               WHERE api='tour' AND op='detailIntro2' AND NOT ok
                               GROUP BY note ORDER BY n DESC LIMIT 10""").fetchall()
        if not rows:
            print("  API 오류 없음 → 실패는 전부 '정상 응답인데 items 가 비어 있음' 이다")
        for r in rows:
            print(f"  {r['n']:>4}× {r['note']}")


if __name__ == "__main__":
    main()
