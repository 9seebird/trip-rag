"""청킹 + 임베딩 + 저장.

  * overview 가 있는 장소만 대상
  * 이미 같은 모델로 만들어 둔 벡터가 있고 overview 가 안 바뀌었으면 건너뛴다
  * 모델을 바꿔 다시 돌리면 그 모델 이름으로 한 벌 더 쌓인다 (실험 비교용)

    docker compose run --rm ingest python -m ingest.build_index
    docker compose run --rm ingest python -m ingest.build_index --model openai/text-embedding-3-small
    docker compose run --rm ingest python -m ingest.build_index --rebuild
"""
import argparse
import time

from .bizrouter import embed, to_pgvector
from .chunking import chunks_for, MAX_CHARS, OVERLAP
from .config import EMBED_MODEL, EMBED_DIM
from .db import connect

PICK = """
SELECT p.content_id, p.content_type, p.title, p.addr, p.overview, p.overview_hash
FROM places p
WHERE p.overview IS NOT NULL AND length(p.overview) > 0
  AND (%(rebuild)s OR NOT EXISTS (
        SELECT 1 FROM chunks c
        WHERE c.content_id = p.content_id AND c.model = %(model)s))
ORDER BY p.priority, p.content_id
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=EMBED_MODEL)
    ap.add_argument("--dim", type=int, default=EMBED_DIM)
    ap.add_argument("--max-chars", type=int, default=MAX_CHARS)
    ap.add_argument("--overlap", type=int, default=OVERLAP)
    ap.add_argument("--batch", type=int, default=32)
    ap.add_argument("--rebuild", action="store_true", help="같은 모델 벡터를 지우고 다시")
    ap.add_argument("--limit", type=int, default=0, help="시험 삼아 N건만")
    ap.add_argument("--no-header", action="store_true",
                    help="조각 앞에 제목을 붙이지 않는다 (대조군)")
    ap.add_argument("--variant", default="",
                    help="같은 모델로 다른 방식의 벡터를 한 벌 더 쌓을 때 붙일 이름")
    args = ap.parse_args()

    # chunks.model 이 벡터 한 벌을 가리키는 이름표다. 변형은 '모델#변형' 으로 구분한다.
    label = args.model + (f"#{args.variant}" if args.variant else "")

    with connect() as conn:
        if args.rebuild:
            n = conn.execute("DELETE FROM chunks WHERE model = %s", (label,)).rowcount
            print(f"기존 {label} 벡터 {n}개 삭제")

        places = conn.execute(PICK, {"model": label, "rebuild": args.rebuild}).fetchall()
        if args.limit:
            places = places[:args.limit]
        if not places:
            print("만들 게 없습니다.")
            return

        # 장소 → 조각 펼치기
        pending = []
        for p in places:
            for seq, text in chunks_for(p, args.max_chars, args.overlap,
                                        with_header=not args.no_header):
                pending.append((p["content_id"], seq, text))

        print(f"장소 {len(places)}건 → 조각 {len(pending)}개")
        print(f"모델 {args.model} · 이름표 {label} · {args.dim}차원"
              f" · 조각 최대 {args.max_chars}자 · 제목접두 {'없음' if args.no_header else '있음'}\n")

        t0 = time.time()
        saved = 0
        for i in range(0, len(pending), args.batch):
            batch = pending[i:i + args.batch]
            vecs = embed(conn, [t for _, _, t in batch], args.model, args.dim)
            with conn.cursor() as cur:
                for (cid, seq, text), v in zip(batch, vecs):
                    cur.execute(
                        """INSERT INTO chunks (content_id, seq, text, model, dim, embedding)
                           VALUES (%s,%s,%s,%s,%s,%s::vector)
                           ON CONFLICT (content_id, seq, model)
                           DO UPDATE SET text = EXCLUDED.text,
                                         embedding = EXCLUDED.embedding,
                                         dim = EXCLUDED.dim,
                                         created_at = now()""",
                        (cid, seq, text, label, args.dim, to_pgvector(v)))
                    saved += 1
            done = min(i + args.batch, len(pending))
            print(f"  {done}/{len(pending)}")

        el = time.time() - t0
        cost = conn.execute(
            """SELECT coalesce(sum(cost_krw),0) c, coalesce(sum(tokens),0) t
               FROM api_calls WHERE api='bizrouter' AND op='embeddings'
                 AND ts > now() - interval '1 hour'""").fetchone()
        print(f"\n조각 {saved}개 저장 · {el:.1f}초")
        print(f"이번 임베딩 토큰 {cost['t']:,} · 비용 {float(cost['c']):.2f}원")

        for r in conn.execute(
                """SELECT model, dim, count(*) chunks, count(DISTINCT content_id) places
                   FROM chunks GROUP BY model, dim ORDER BY model""").fetchall():
            print(f"  {r['model']} ({r['dim']}차원): 조각 {r['chunks']:,} · 장소 {r['places']:,}")


if __name__ == "__main__":
    main()
