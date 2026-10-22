"""조건 뽑기 회귀 테스트 — 2차 평가에서 드러난 오탐이 다시 생기지 않게.

    docker compose run --rm ingest python -m eval.test_extract
    (DB·API 없이 돈다. 로컬 python 으로도 가능: python -m eval.test_extract)
"""
from app.extract import extract

# (질문, 기대 조건의 부분집합)
CASES = [
    # 오탐 — 2차 평가 q01·q06·q20 을 0점으로 만든 것들
    ("아이스뮤지엄 얼음 조각",            {"baby": False, "types": [12, 14]}),
    ("아기자기한 소품샵",                 {"baby": False}),
    ("사회적 기업 카페",                  {"types": [39]}),
    ("카펫 깔린 한옥",                    {"pet": False}),
    # 제대로 잡아야 하는 것들
    ("비 오는 날 아이랑 갈 만한 실내",     {"indoor": True, "baby": True}),
    ("아이랑 체험할 수 있는 박물관",       {"baby": True, "types": [12, 14]}),
    ("애기 데리고 갈 곳",                 {"baby": True}),
    ("어린이 놀이터",                     {"baby": True}),
    ("아이와 함께",                       {"baby": True}),
    ("강아지 데리고 갈 수 있는 카페",       {"pet": True, "types": [39]}),
    ("펫 동반 가능한 숙소",               {"pet": True}),
    # q14 — 체험은 레포츠만이 아니다
    ("날씨 상관없이 즐길 수 있는 실내 체험", {"indoor": True, "types": [12, 14, 28]}),
    ("제주 흑돼지 맛집",                  {"area": 39, "types": [39]}),
    ("해산물 정식 잘하는 집",             {"types": [39]}),
    # 지역 — 부산·강원 추가
    ("해운대 근처 카페",                  {"area": 6, "types": [39]}),
    ("강릉 바다 보이는 곳",               {"area": 32}),
    ("동해 바다 일출",                    {"area": None}),
    ("천지연폭포",                        {"baby": False, "pet": False, "indoor": None, "types": None}),
    # 지역 — 전국 확장
    ("서울 비 오는 날 실내 데이트",        {"area": 1, "indoor": True}),
    ("세종문화회관 근처 맛집",             {"area": 1, "types": [39]}),
    ("인천 차이나타운 짜장면 맛집",         {"area": 2, "types": [39]}),
    ("대전 성심당 빵",                    {"area": 3}),
    ("대구 동성로 카페",                  {"area": 4, "types": [39]}),
    ("광주 무등산 등산",                  {"area": 5}),
    ("경기 광주 곤지암 리조트",            {"area": 31}),
    ("울산 간절곶 일출",                  {"area": 7}),
    ("세종 호수공원 산책",                {"area": 8}),
    ("가평 아이랑 갈 만한 곳",             {"area": 31, "baby": True}),
    ("청주 박물관",                       {"area": 33, "types": [12, 14]}),
    ("공주 공산성 야경",                  {"area": 34}),
    ("경주 맛집",                         {"area": 35, "types": [39]}),
    ("통영 케이블카",                     {"area": 36}),
    ("전주 한옥마을",                     {"area": 37}),
    ("여수 밤바다",                       {"area": 38}),
    ("설악산 울산바위 등산",               {"area": 32}),
    ("송도 해수욕장 산책",                {"area": 6}),
    # 지명과 같은 흔한 낱말 — 지역으로 잡으면 안 된다
    ("고양이랑 갈 수 있는 카페",           {"area": None, "types": [39]}),
    ("예산 적게 드는 여행",               {"area": None}),
    ("양주 한 잔 하기 좋은 바",            {"area": None}),
    ("세종대왕 동상",                     {"area": None}),
    ("유성우 보러 가기 좋은 곳",           {"area": None}),
    ("2차 세계대전 기념관",               {"area": None, "types": [12, 14]}),
    ("의미를 부여하는 여행",              {"area": None}),
    ("공주처럼 사진 찍는 곳",             {"area": None}),
]


def main():
    bad = 0
    for q, want in CASES:
        got = extract(q)["filters"]
        diff = {k: (got.get(k), v) for k, v in want.items() if got.get(k) != v}
        mark = "ok " if not diff else "FAIL"
        bad += bool(diff)
        print(f"  {mark} {q}" + (f"   {diff}" if diff else ""))
    print(f"\n{len(CASES) - bad}/{len(CASES)} 통과")
    raise SystemExit(1 if bad else 0)


def test_cases():            # pytest 로 돌려도 되게
    for q, want in CASES:
        got = extract(q)["filters"]
        assert all(got.get(k) == v for k, v in want.items()), (q, got)


if __name__ == "__main__":
    main()
