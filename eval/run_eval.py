"""평가 — 뭘 바꿨더니 얼마나 좋아졌나를 숫자로.

재는 것
  recall@k : 정답 중 몇 개가 상위 k 안에 들어왔나 (검색이 틀리면 뒤는 볼 필요 없다)
  MRR@10   : 첫 정답이 몇 번째로 나왔나 (1등과 8등은 체감이 다르다)
  응답시간 : 설정마다 얼마나 느려지는가. 좋아진 만큼 비싸졌는지 봐야 한다

    docker compose run --rm ingest python -m eval.run_eval
    docker compose run --rm ingest python -m eval.run_eval --configs 벡터만,벡터+리랭커
"""
import argparse
import pathlib
import statistics
import time

import yaml

from app.extract import extract
from app.search import (keyword_search, vector_candidates, apply_rerank, filtered_candidates,
                        hybrid_candidates, rrf)
from ingest.config import EMBED_MODEL
from ingest.db import connect

HERE = pathlib.Path(__file__).resolve().parent
LABELS = HERE / "labels.yaml"
RESULTS = HERE / "results.md"
K = (1, 3, 5, 10)


# ── 설정: 이름 → (질문, conn) 을 받아 content_id 순위 리스트를 돌려주는 함수 ──────
def make_configs(conn, available_models):
    cfg = {}

    cfg["키워드만"] = lambda q: [r["content_id"] for r in keyword_search(conn, q, 10)]
    cfg["벡터만"] = lambda q: [r["content_id"] for r in vector_candidates(conn, q, 10)]
    # 조건 필터가 붙은 설정은 화면과 같은 경로(filtered_candidates)를 탄다. 필터 완화까지 포함.
    cfg["벡터+조건필터"] = lambda q: [
        r["content_id"] for r in filtered_candidates(conn, q, 10, extract(q)["filters"])[0]]
    cfg["벡터+리랭커"] = lambda q: [
        r["content_id"] for r in apply_rerank(conn, q, vector_candidates(conn, q, 30), 10)]
    cfg["벡터+조건필터+리랭커"] = lambda q: [
        r["content_id"] for r in apply_rerank(
            conn, q, filtered_candidates(conn, q, 30, extract(q)["filters"])[0], 10)]

    # 하이브리드 — 서비스 화면(/api/find)이 쓰는 방식. 키워드와 의미의 순위를 RRF 로 합친다
    cfg["하이브리드"] = lambda q: [
        r["content_id"] for r in rrf(vector_candidates(conn, q, 30), keyword_search(conn, q, 30))[:10]]
    cfg["하이브리드+조건필터"] = lambda q: [
        r["content_id"] for r in hybrid_candidates(conn, q, 30, extract(q)["filters"])[0][:10]]

    # 제목 접두 대조군 (build_index --no-header --variant noheader 로 만들어 둔 경우에만)
    noheader = f"{EMBED_MODEL}#noheader"
    if noheader in available_models:
        cfg["벡터만(제목접두 없음)"] = lambda q: [
            r["content_id"] for r in vector_candidates(conn, q, 10, None, noheader)]

    # 다른 임베딩 모델 (build_index --model ... 로 만들어 둔 경우에만)
    for m in available_models:
        if m != EMBED_MODEL and "#" not in m:
            cfg[f"벡터만({m.split('/')[-1]})"] = (
                lambda q, mm=m: [r["content_id"] for r in vector_candidates(conn, q, 10, None, mm)])
    return cfg


def score(ranked, expect):
    """recall@k · Success@k · 역순위.

    정답이 k 개보다 많으면 recall@k 의 최대값이 k/정답수 로 눌린다.
    장소 추천처럼 정답이 여럿인 과제에서는 Success@k(정답이 하나라도 상위 k 에 있었나)가
    체감에 더 가깝다. 그래서 둘 다 낸다.
    """
    exp = set(expect)
    out = {f"recall@{k}": len(exp & set(ranked[:k])) / len(exp) for k in K}
    out.update({f"S@{k}": 1.0 if exp & set(ranked[:k]) else 0.0 for k in K})
    rr = 0.0
    for i, cid in enumerate(ranked[:10], 1):
        if cid in exp:
            rr = 1 / i
            break
    out["MRR"] = rr
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--configs", default="", help="쉼표로. 비우면 전부")
    ap.add_argument("--kind", default="", help="situation / condition / proper 만")
    args = ap.parse_args()

    if not LABELS.exists():
        print("eval/labels.yaml 이 없습니다. 먼저 build_pool 을 돌리세요.")
        return
    items = [x for x in yaml.safe_load(LABELS.read_text(encoding="utf-8")) if x.get("expect")]
    if args.kind:
        items = [x for x in items if x.get("kind") == args.kind]
    if not items:
        print("정답이 있는 질문이 없습니다.")
        return

    with connect() as conn:
        models = [r["model"] for r in conn.execute(
            "SELECT DISTINCT model FROM chunks ORDER BY model").fetchall()]
        configs = make_configs(conn, models)
        if args.configs:
            keep = {s.strip() for s in args.configs.split(",")}
            configs = {k: v for k, v in configs.items() if k in keep}

        reviewed = sum(1 for x in items if x.get("reviewed"))
        print(f"질문 {len(items)}개 (검수 완료 {reviewed}개) · 설정 {len(configs)}개\n")

        rows, detail = [], {}
        for name, fn in configs.items():
            per, times = [], []
            for it in items:
                t0 = time.time()
                try:
                    ranked = fn(it["q"])
                except Exception as e:
                    print(f"  ! {name} / {it['id']}: {str(e)[:120]}")
                    ranked = []
                times.append((time.time() - t0) * 1000)
                sc = score(ranked, [e["id"] for e in it["expect"]])
                per.append(sc)
                detail[(name, it["id"])] = sc
            agg = {k: statistics.mean(p[k] for p in per) for k in per[0]}
            agg["ms"] = statistics.median(times)
            agg["name"] = name
            rows.append(agg)
            print(f"  {name:<26} S@5 {agg['S@5']:.3f} · recall@5 {agg['recall@5']:.3f}"
                  f" · MRR {agg['MRR']:.3f} · {agg['ms']:.0f}ms")

        base = next((r for r in rows if r["name"] == "벡터만"), rows[0])
        lines = [
            "# 실험 결과", "",
            f"질문 {len(items)}개 · 장소 879건 · 임베딩 `{EMBED_MODEL}` 1024차원", "",
            "| 설정 | S@1 | S@3 | S@5 | recall@5 | recall@10 | MRR@10 | 중앙 응답시간 | S@5 기준 대비 |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
        for r in rows:
            d = r["S@5"] - base["S@5"]
            delta = "기준" if r is base else f"{d:+.3f}"
            lines.append(
                f"| {r['name']} | {r['S@1']:.3f} | {r['S@3']:.3f} | {r['S@5']:.3f}"
                f" | {r['recall@5']:.3f} | {r['recall@10']:.3f} | {r['MRR']:.3f}"
                f" | {r['ms']:.0f}ms | {delta} |")
        lines += ["", "S@k = 정답이 하나라도 상위 k 안에 들어온 질문의 비율. "
                      "정답이 여럿인 추천 과제에서는 이쪽이 체감에 가깝다.",
                  "recall@k = 정답 중 몇 개가 상위 k 안에 들어왔는가. "
                  "정답 수가 k 보다 많으면 최대값이 눌리므로 절대값보다 설정 간 차이를 볼 것."]

        lines += ["", "## 질문 유형별 S@5", "",
                  "| 설정 | 상황(situation) | 조건(condition) | 고유명사(proper) |", "|---|---:|---:|---:|"]
        kinds = ["situation", "condition", "proper"]
        for name, fn in configs.items():
            cells = []
            for kind in kinds:
                sub = [x for x in items if x.get("kind") == kind]
                if not sub:
                    cells.append("—"); continue
                vals = []
                for it in sub:
                    try:
                        ranked = fn(it["q"])
                    except Exception:
                        ranked = []
                    vals.append(score(ranked, [e["id"] for e in it["expect"]])["S@5"])
                cells.append(f"{statistics.mean(vals):.3f}")
            lines.append(f"| {name} | " + " | ".join(cells) + " |")

        # 기준(벡터만)보다 나빠진 질문을 드러낸다. 평균만 보면 놓치는 것들.
        lines += ["", "## 기준(벡터만)보다 나빠진 질문", "",
                  "평균이 올랐어도 특정 질문에서 크게 잃었다면 그게 더 중요한 정보다.", ""]
        for name in configs:
            if name == base["name"]:
                continue
            worse = [(it, detail[(base["name"], it["id"])]["recall@5"],
                      detail[(name, it["id"])]["recall@5"])
                     for it in items
                     if detail[(name, it["id"])]["recall@5"] < detail[(base["name"], it["id"])]["recall@5"]]
            if not worse:
                lines.append(f"- **{name}**: 나빠진 질문 없음")
                continue
            lines.append(f"- **{name}**: {len(worse)}개 질문에서 하락")
            for it, b, a in sorted(worse, key=lambda x: x[1] - x[2], reverse=True)[:5]:
                lines.append(f"    - `{it['id']}` {it['q']} — {b:.2f} → {a:.2f}")

        lines += ["", "> 정답은 여러 검색 방식으로 후보를 모아(pooling) 모델이 판정하고 사람이 검수한 것이다.",
                  "> 후보 풀에 한 번도 안 들어온 장소는 정답이 될 수 없으므로, recall 절대값은 낙관적으로 나온다.",
                  "> 설정 간 **비교**는 유효하지만 절대 성능으로 읽으면 안 된다."]
        RESULTS.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"\neval/results.md 저장")


if __name__ == "__main__":
    main()
