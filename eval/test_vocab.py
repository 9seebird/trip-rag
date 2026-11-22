"""검색어 손질(app/vocab.py) 회귀 테스트 — 줄임말 넓히기와 '데이터에 없는 말' 알아채기.

    docker compose run --rm ingest python -m eval.test_vocab
    (DB·API 없이 돈다. 데이터는 아래 WORDS 로 흉내 낸다)
"""
from app import vocab
from app.extract import extract
from app.search import tokens

# 데이터에 '있는' 말이라고 치는 것들 (실제 DB 에서는 search.has_any 가 이름·소개글을 본다)
WORDS = ["맥주", "카페", "실내", "데이트", "산책", "야경", "명소", "전통", "캠핑", "전주", "파는"]


def has_any(ws):
    return any(w in WORDS for w in ws)


# (질문, 넓힌 낱말, 없는 말, weak)
CASES = [
    ("전주 가맥집",           ["가맥집"],   [],          False),   # 줄임말 → 맥주로 넓혀 찾는다
    ("카공하기 좋은 카페",    ["카공하기"], [],          False),   # 끝꼴(하기)이 붙어도 사전에서 찾는다
    ("서울 노포",             ["노포"],     [],          False),
    ("전일갑오",              [],           ["전일갑오"], True),    # 앞 두 글자('전일')만 맞는다고 통과시키지 않는다
    ("전주 전일갑오",         [],           ["전일갑오"], True),    # 지역은 조건으로 빠지고 찾는 말은 없는 말
    ("두쫀쿠 파는 곳",        [],           ["두쫀쿠"],   False),   # 일부만 없는 말 → 접지 않고 한 줄 안내
    ("비 오는 날 아이랑 갈 만한 실내", [],  [],          False),   # 평범한 질문은 손대지 않는다
    ("전주 데이트하러",       [],           [],          False),   # '하러' 를 떼면 데이터에 있는 말
    ("가맥집 추천좀",         ["가맥집"],   [],          False),   # '추천좀' 같은 부탁 말투는 찾는 말이 아니다
    ("가맥집 괜찮은데 있어?", ["가맥집"],   [],          False),
    ("광명시 카페 추천 해줄래", [],         [],          False),   # 말하듯 쓴 부탁(해줄래)은 '없는 말' 이 아니다
]

# 넓혀 찾았어도 그 말 그대로는 데이터에 없는 것 (화면이 카카오 결과를 위로 올린다)
ABSENT = [("전주 가맥집", ["가맥집"]), ("서울 노포", ["노포"]), ("야경 명소", [])]


def run_case(q):
    ex = extract(q)
    return vocab.judge(vocab.content_tokens(tokens(q), ex["why"]), has_any)


def main():
    bad = 0
    for q, exp, unk, weak in CASES:
        j = run_case(q)
        got = (sorted(j["expanded"]), j["unknown"], j["weak"])
        ok = got == (sorted(exp), unk, weak)
        bad += not ok
        print(f"  {'ok ' if ok else 'FAIL'} {q}" + ("" if ok else f"   got={got}"))
    for q, want in ABSENT:
        got = run_case(q)["absent"]
        ok = got == want
        bad += not ok
        print(f"  {'ok ' if ok else 'FAIL'} absent {q} → {got}")
    q = vocab.search_text("전주 가맥집", {"가맥집": vocab.SLANG["가맥"]})
    ok = q == "전주 가맥집 맥주 호프 주점 포차"
    bad += not ok
    print(f"  {'ok ' if ok else 'FAIL'} search_text → {q}")
    n = len(CASES) + len(ABSENT) + 1
    print(f"\n{n - bad}/{n} 통과")
    raise SystemExit(1 if bad else 0)


def test_cases():            # pytest 로 돌려도 되게
    for q, exp, unk, weak in CASES:
        j = run_case(q)
        assert (sorted(j["expanded"]), j["unknown"], j["weak"]) == (sorted(exp), unk, weak), (q, j)


if __name__ == "__main__":
    main()
