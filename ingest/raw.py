"""상세 오퍼레이션 진단 — 원본 응답을 그대로 찍어 본다.

probe 에서 detailCommon 계열이 전부 실패했을 때, 진짜 오류 메시지를 보려고 쓴다.
후보를 파라미터 조합까지 바꿔가며 시도하고 응답 앞부분을 그대로 출력한다.

    docker compose run --rm ingest python -m ingest.raw 126439
"""
import sys
import urllib.parse
import httpx

from .config import TOUR_API_KEY, TOUR_BASE, TOUR_APP_NAME

CANDIDATES = [
    ("detailCommon2", {"contentId": None, "contentTypeId": 12}),
    ("detailCommon2", {"contentId": None}),
    ("detailCommon2", {"contentId": None, "defaultYN": "Y", "overviewYN": "Y",
                       "firstImageYN": "Y", "addrinfoYN": "Y", "mapinfoYN": "Y"}),
    ("detailIntro2",  {"contentId": None, "contentTypeId": 12}),
    ("detailInfo2",   {"contentId": None, "contentTypeId": 12}),
    ("detailImage2",  {"contentId": None, "imageYN": "Y"}),
]


def key():
    k = TOUR_API_KEY.strip()
    return urllib.parse.unquote(k) if "%" in k else k


def main():
    cid = sys.argv[1] if len(sys.argv) > 1 else "126439"
    for op, params in CANDIDATES:
        p = {k: (cid if v is None else v) for k, v in params.items()}
        q = {"serviceKey": key(), "MobileOS": "ETC", "MobileApp": TOUR_APP_NAME,
             "_type": "json", **p}
        url = f"{TOUR_BASE}/{op}"
        print("=" * 70)
        print(op, {k: v for k, v in p.items()})
        try:
            r = httpx.get(url, params=q, timeout=20)
            print(f"HTTP {r.status_code}")
            print(r.text[:700].replace("\n", " "))
        except Exception as e:
            print("EXC", e)
        print()


if __name__ == "__main__":
    main()
