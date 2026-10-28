"""검색엔진이 읽을 수 있는 고정 페이지 (서버에서 HTML 을 다 그려서 보낸다).

첫 화면(index.html)은 버튼을 눌러야 내용이 그려져서 구글·네이버에는 첫 화면 한 장만 잡힌다.
여기 페이지들은 주소마다 내용이 들어 있어 검색에 걸리고, 매일 내용이 바뀐다.

  /area                 17개 시도 목록
  /area/{slug}          그 지역의 이번 주말: 날씨·축제·여유로운 곳·실내·맛집
  /festival             → 이번 달
  /festival/{YYYY-MM}   그달 열리는 축제 (지역별)
  /theme, /theme/{slug} 비 오는 날 실내, 단풍 명소 같은 주제별 모음
  /place/{content_id}   장소 한 곳 (사진·소개·운영시간·근처 장소)
  /sitemap.xml          위 주소 전부

장소 고르기는 날짜를 씨앗으로 섞어(md5) 하루 동안은 같은 목록, 다음 날은 다른 목록이 되게 한다.
"""
import html
import json
import re
from urllib.parse import quote
from datetime import date, datetime, timedelta

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from ingest.db import connect
from . import cache, weather
from .extract import AREA_NAME
from .weather import KST

router = APIRouter()
SITE = "https://trip.trendkit.app"

AREAS = [(1, "seoul"), (2, "incheon"), (31, "gyeonggi"), (32, "gangwon"), (3, "daejeon"), (8, "sejong"),
         (33, "chungbuk"), (34, "chungnam"), (4, "daegu"), (35, "gyeongbuk"), (6, "busan"), (7, "ulsan"),
         (36, "gyeongnam"), (5, "gwangju"), (37, "jeonbuk"), (38, "jeonnam"), (39, "jeju")]
SLUG = dict(AREAS)
CODE = {s: c for c, s in AREAS}
LONG = {1: "서울", 2: "인천", 31: "경기도", 32: "강원도", 3: "대전", 8: "세종", 33: "충청북도", 34: "충청남도",
        4: "대구", 35: "경상북도", 6: "부산", 7: "울산", 36: "경상남도", 5: "광주", 37: "전라북도",
        38: "전라남도", 39: "제주도"}
CENTER = {1: (37.566, 126.978), 2: (37.456, 126.705), 3: (36.350, 127.385), 4: (35.871, 128.601),
          5: (35.160, 126.853), 6: (35.180, 129.076), 7: (35.538, 129.311), 8: (36.480, 127.289),
          31: (37.264, 127.029), 32: (37.751, 128.876), 33: (36.642, 127.489), 34: (36.601, 126.660),
          35: (36.568, 128.729), 36: (35.228, 128.681), 37: (35.824, 127.148), 38: (34.816, 126.463),
          39: (33.500, 126.531)}
TYPE = {12: "관광지", 14: "문화시설", 15: "축제·행사", 28: "레포츠", 39: "음식점"}
DOW = "월화수목금토일"

# 주제 모음. where 는 places p 에 거는 조건 (텍스트 조건은 제목+소개글에서 찾는다)
TXT = "(p.title || ' ' || coalesce(p.overview, ''))"
THEMES = [
    {"slug": "rainy-day-indoor", "title": "비 오는 날 가기 좋은 실내 명소", "short": "비 오는 날 실내",
     "q": "비 오는 날 가기 좋은 실내",
     "intro": "비가 오거나 너무 덥고 추운 날에도 일정을 바꾸지 않아도 되는 실내 관광지와 문화시설을 모았습니다. "
              "박물관·미술관·전시관·실내 체험 시설처럼 날씨 영향을 덜 받는 곳 위주예요.",
     "where": "p.indoor IS TRUE AND p.content_type IN (12, 14)"},
    {"slug": "with-kids", "title": "아이랑 가기 좋은 곳", "short": "아이랑",
     "q": "아이랑 가기 좋은 체험",
     "intro": "아이와 함께 반나절을 보내기 좋은 체험 시설, 과학관, 동물원·아쿠아리움, 놀이 공간을 모았습니다. "
              "유모차 대여가 되는 곳도 함께 표시해 두었어요.",
     "where": f"p.content_type IN (12, 14, 28) AND (p.baby_ok IS TRUE OR p.stroller_ok IS TRUE "
              f"OR {TXT} ~ '어린이|키즈|체험관|과학관|동물원|아쿠아리움|놀이')"},
    {"slug": "with-pet", "title": "반려견과 함께 가는 여행지", "short": "반려견 동반",
     "q": "반려견이랑 갈 수 있는 곳",
     "intro": "한국관광공사 반려동물 동반여행 정보에 반려동물 동반이 된다고 올라온 곳을 모았습니다. "
              "동반 조건(크기·목줄·실내 출입)은 장소마다 달라서, 장소 페이지에서 꼭 확인해 주세요.",
     "where": "p.pet_allowed IS TRUE"},
    {"slug": "barrier-free", "title": "휠체어·유모차로 다니기 좋은 무장애 여행지", "short": "무장애",
     "q": "휠체어로 갈 수 있는 곳",
     "intro": "한국관광공사 무장애 여행 정보에 올라온 곳 가운데 휠체어 대여나 경사로·장애인 화장실 정보가 있는 곳을 모았습니다. "
              "어르신, 유모차와 함께하는 여행에도 참고하기 좋아요.",
     "where": "(p.wheelchair_ok IS TRUE OR p.access_listed IS TRUE) AND p.content_type IN (12, 14)"},
    {"slug": "autumn-foliage", "title": "단풍 명소", "short": "단풍 명소",
     "q": "단풍 보기 좋은 곳",
     "intro": "소개글에 단풍이 아름답다고 적힌 산·사찰·공원·숲길을 모았습니다. 단풍 절정은 해마다 다르니 "
              "가기 전에 그 지역 단풍 소식도 함께 확인해 보세요.",
     "where": f"p.content_type = 12 AND {TXT} ~ '단풍'"},
    {"slug": "night-view", "title": "야경 명소", "short": "야경",
     "q": "야경 보기 좋은 곳",
     "intro": "해 진 뒤에 더 예쁜 곳들입니다. 전망대, 다리, 성곽, 야간 개장하는 공원처럼 소개글에 야경이 언급된 곳을 모았어요.",
     "where": f"p.content_type IN (12, 14) AND {TXT} ~ '야경'"},
    {"slug": "ocean-walk", "title": "바다 보며 걷는 산책길", "short": "바다 산책길",
     "q": "바다 보이는 산책길",
     "intro": "해안 산책로, 바닷길, 해변 둘레길처럼 바다를 옆에 두고 걷기 좋은 길을 모았습니다. "
              "바람이 센 날이 많으니 겉옷을 챙기세요.",
     "where": f"p.content_type = 12 AND {TXT} ~ '해안|바다|해변' AND {TXT} ~ '산책|둘레길|탐방로|데크|걷기'"},
    {"slug": "hot-spring", "title": "온천·스파", "short": "온천·스파",
     "q": "온천 스파",
     "intro": "쌀쌀한 날 몸을 녹이기 좋은 온천과 스파, 찜질 시설을 모았습니다.",
     "where": f"p.content_type IN (12, 28) AND {TXT} ~ '온천|스파'"},
    {"slug": "museum", "title": "박물관·미술관", "short": "박물관·미술관",
     "q": "박물관 미술관",
     "intro": "전국의 박물관, 미술관, 기념관을 모았습니다. 대부분 월요일에 쉬니 운영일을 꼭 확인하세요.",
     "where": "p.content_type = 14 AND p.title ~ '박물관|미술관|기념관|갤러리'"},
    {"slug": "ocean-cafe", "title": "바다 보이는 카페", "short": "바다 카페",
     "q": "바다 보면서 커피 마실 곳",
     "intro": "창밖으로 바다가 보이는 카페를 모았습니다. 해 질 녘에 가면 노을까지 볼 수 있는 곳이 많아요.",
     "where": f"p.content_type = 39 AND (p.cat3 = 'A05020900' OR p.title ~* '카페|커피|cafe|coffee') "
              f"AND {TXT} ~ '바다|오션|해변|바닷가'"},
]
THEME = {t["slug"]: t for t in THEMES}


# ── 공통 ──────────────────────────────────────────────────────────────────────
e = lambda s: html.escape(str(s or ""), quote=True)


def today():
    return datetime.now(KST).date()


def https(u):
    return re.sub(r"^http://", "https://", u or "")


def clean(t):
    """TourAPI 글에 섞인 <br>·태그·엔티티 정리 → 줄바꿈 있는 평문."""
    t = re.sub(r"<br\s*/?>", "\n", t or "", flags=re.I)
    t = re.sub(r"<[^>]+>", "", t)
    return html.unescape(t).strip()


def md(d):
    return f"{d.month}.{d.day}" if d else ""


def weekend(t=None):
    t = t or today()
    if t.weekday() == 6:                         # 일요일이면 오늘 하루
        return t, t
    sat = t + timedelta(days=(5 - t.weekday()) % 7)
    return sat, sat + timedelta(days=1)


def shuffle_sql():
    return f"md5(p.content_id::text || '{today().isoformat()}')"


CSS = """
:root{--bg:#f6f5f1;--surface:#fff;--surface-2:#f0efe9;--ink:#1b1c1a;--ink-2:#4a4b47;--muted:#7a7a73;--line:#e4e2da;
--accent:#0f766e;--accent-soft:#e3f1ee;--warm:#ea580c;--warm-soft:#fdeee3;--blue:#2563eb;--olive:#4d7c0f;--olive-soft:#eef5e1}
@media (prefers-color-scheme:dark){:root{--bg:#121412;--surface:#1b1e1c;--surface-2:#232725;--ink:#ecece6;--ink-2:#c4c5bd;
--muted:#91928a;--line:#2e3330;--accent:#2dd4bf;--accent-soft:#12302c;--warm:#fb923c;--warm-soft:#3a2416;--blue:#7aa2f7;
--olive:#a3d160;--olive-soft:#223018}}
*{box-sizing:border-box}html{-webkit-text-size-adjust:100%}html,body{overflow-x:clip}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.65 "Pretendard Variable",Pretendard,-apple-system,BlinkMacSystemFont,"Apple SD Gothic Neo","Malgun Gothic",sans-serif;letter-spacing:-.01em;word-break:keep-all;min-height:100vh;display:flex;flex-direction:column}
a{color:inherit}
.wrap{max-width:1180px;margin:0 auto;padding:0 24px;width:100%}
@media (max-width:600px){.wrap{padding:0 16px}}
.top{display:flex;align-items:center;justify-content:space-between;height:60px;gap:12px}
.brand{display:flex;align-items:center;gap:9px;font-weight:700;font-size:18px;text-decoration:none;flex:none}
.brand i{width:28px;height:28px;border-radius:8px;background:var(--accent);display:grid;place-items:center}
.brand small{font-weight:500;color:var(--muted);font-size:13px}
.top nav{display:flex;gap:2px;overflow-x:auto;scrollbar-width:none}
.top nav a{font-size:13.5px;color:var(--ink-2);text-decoration:none;padding:6px 10px;border-radius:8px;white-space:nowrap}
.top nav a:hover,.top nav a.on{background:var(--surface-2);color:var(--ink)}
@media (max-width:600px){.brand small{display:none}.top nav a{padding:6px 7px;font-size:13px}.top nav a.home{display:none}}
main{flex:1 0 auto}
.crumb{font-size:13px;color:var(--muted);margin:14px 0 6px}.crumb a{text-decoration:none;color:var(--ink-2)}
h1{font-size:clamp(25px,4vw,34px);letter-spacing:-.035em;line-height:1.25;margin:6px 0 8px}
.lead{color:var(--ink-2);margin:0 0 18px;max-width:760px}
h2{font-size:19px;letter-spacing:-.025em;margin:34px 0 12px;display:flex;align-items:baseline;gap:8px;flex-wrap:wrap}
h2 small{font-size:12.5px;color:var(--muted);font-weight:500}
.grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px}
@media (max-width:899px){.grid{grid-template-columns:repeat(3,minmax(0,1fr))}}
@media (max-width:600px){.grid{grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}}
.card{display:flex;flex-direction:column;text-decoration:none;background:var(--surface);border:1px solid var(--line);border-radius:14px;overflow:hidden;min-width:0}
.card:hover{border-color:var(--accent)}
.ph{position:relative;aspect-ratio:16/10;background:var(--surface-2) linear-gradient(135deg,var(--accent-soft),var(--surface-2));overflow:hidden}
.ph img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover}
.ph .b{position:absolute;left:8px;top:8px;padding:2px 8px;border-radius:999px;font-size:11.5px;font-weight:700;background:var(--olive-soft);color:var(--olive)}
.ph .b.f{background:var(--warm-soft);color:var(--warm)}
.card .t{padding:9px 11px 11px;display:flex;flex-direction:column;gap:2px;min-width:0}
.card b{font-size:14.5px;line-height:1.35;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.card small{color:var(--muted);font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.chips{display:flex;flex-wrap:wrap;gap:6px}
.chip{display:inline-flex;align-items:center;gap:4px;padding:7px 13px;border-radius:999px;border:1px solid var(--line);background:var(--surface);text-decoration:none;font-size:13.5px}
.chip:hover,.chip.on{border-color:var(--accent);color:var(--accent)}
.chip.on{background:var(--accent-soft);font-weight:600}
.cta{display:inline-flex;align-items:center;gap:6px;background:var(--accent);color:#fff;text-decoration:none;font-weight:700;border-radius:12px;padding:11px 18px;font-size:14.5px}
@media (prefers-color-scheme:dark){.cta{color:#06201d}}
.btn{display:inline-flex;align-items:center;gap:6px;border:1px solid var(--line);background:var(--surface);text-decoration:none;font-weight:600;border-radius:12px;padding:10px 16px;font-size:14px}
.acts{display:flex;gap:8px;flex-wrap:wrap;margin:16px 0}
.wx{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px;max-width:560px}
.wx div{background:var(--surface);border:1px solid var(--line);border-radius:14px;padding:12px 14px;display:flex;align-items:center;gap:12px}
.wx i{font-style:normal;font-size:28px}.wx b{display:block}.wx small{color:var(--muted);font-size:12.5px}
.box{background:var(--surface);border:1px solid var(--line);border-radius:16px;padding:18px 20px}
.facts{display:grid;grid-template-columns:auto 1fr;gap:7px 16px;font-size:14px;margin:0}
.facts dt{color:var(--muted);white-space:nowrap}.facts dd{margin:0;white-space:pre-line;overflow-wrap:anywhere}
.ov{white-space:pre-line;color:var(--ink-2);font-size:15.5px;line-height:1.85}
.hero{aspect-ratio:16/8;border-radius:18px;overflow:hidden;background:var(--surface-2);position:relative;margin:8px 0 18px}
.hero img{width:100%;height:100%;object-fit:cover}
@media (max-width:600px){.hero{aspect-ratio:4/3;border-radius:14px}}
.cols{display:grid;grid-template-columns:minmax(0,1.6fr) minmax(0,1fr);gap:22px;align-items:start}
@media (max-width:899px){.cols{grid-template-columns:1fr}}
.tag{display:inline-block;padding:2px 9px;border-radius:999px;font-size:12px;font-weight:700;background:var(--accent-soft);color:var(--accent);margin-right:4px}
.mon{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin:6px 0 4px}
.empty{color:var(--muted);padding:20px 0}
.adfit{display:flex;justify-content:center;align-items:center;margin:36px auto 6px;min-height:90px;width:100%;overflow:hidden}
.adfit.empty{display:none}
@media (max-width:799px){.adfit{min-height:100px}}
footer{border-top:1px solid var(--line);margin-top:40px;padding:22px 0 40px;font-size:12.5px;color:var(--muted);line-height:1.8}
footer nav{display:flex;flex-wrap:wrap;gap:4px 14px;margin-bottom:8px}footer nav a{color:var(--ink-2);text-decoration:none;font-weight:600}
"""

AD_JS = """(function(){var box=document.getElementById("adfit");if(!box)return;
var m=window.matchMedia("(max-width: 799px)").matches;
var ad=m?{unit:"DAN-05aYFOoiSIa6Ch4w",w:320,h:100}:{unit:"DAN-30fMubWb77Sswjvi",w:728,h:90};
window.__banajeolAdFail=function(){box.classList.add("empty");};
var ins=document.createElement("ins");ins.className="kakao_ad_area";ins.style.display="block";ins.style.maxWidth="100%";
ins.setAttribute("data-ad-unit",ad.unit);ins.setAttribute("data-ad-width",String(ad.w));ins.setAttribute("data-ad-height",String(ad.h));
ins.setAttribute("data-ad-onfail","__banajeolAdFail");box.appendChild(ins);
var s=document.createElement("script");s.async=true;s.src="https://t1.kakaocdn.net/kas/static/ba.min.js";document.body.appendChild(s);})();"""

LOGO = ("<svg width='16' height='16' viewBox='0 0 32 32' aria-hidden='true'><path d='M8 21a8 8 0 0 1 16 0' fill='none' "
        "stroke='#fff' stroke-width='2.6' stroke-linecap='round'/><circle cx='16' cy='12' r='2.4' fill='#fdba74'/></svg>")


def page(title, desc, path, body, *, jsonld=None, image=None, nav=None, noindex=False):
    url = SITE + path
    ld = "".join(f'<script type="application/ld+json">{json.dumps(j, ensure_ascii=False)}</script>'
                 for j in (jsonld or []))
    links = [("/area", "지역별 주말", "area"), ("/festival", "축제 달력", "festival"),
             ("/theme", "테마 여행", "theme"), ("/", "말로 찾기", "home")]
    navh = "".join(f'<a href="{h}" class="{k} {"on" if k == nav else ""}">{t}</a>' for h, t, k in links)
    return f"""<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>{e(title)} | 반나절</title><meta name="description" content="{e(desc)}">
<link rel="canonical" href="{url}">{'<meta name="robots" content="noindex">' if noindex else ''}
<meta property="og:title" content="{e(title)}"><meta property="og:description" content="{e(desc)}">
<meta property="og:type" content="website"><meta property="og:url" content="{url}"><meta property="og:locale" content="ko_KR">
<meta property="og:site_name" content="반나절">{f'<meta property="og:image" content="{e(https(image))}">' if image else ''}
<meta name="theme-color" content="#0f766e"><link rel="manifest" href="/manifest.webmanifest">
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='8' fill='%230f766e'/%3E%3Cpath d='M8 21a8 8 0 0 1 16 0' fill='none' stroke='%23fff' stroke-width='2.6' stroke-linecap='round'/%3E%3Ccircle cx='16' cy='12' r='2.4' fill='%23fdba74'/%3E%3C/svg%3E">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/gh/orioncactus/pretendard@v1.3.9/dist/web/variable/pretendardvariable-dynamic-subset.min.css">
<style>{CSS}</style>{ld}</head><body>
<div class="wrap"><header class="top"><a class="brand" href="/"><i>{LOGO}</i>반나절 <small>말로 찾는 여행 코스</small></a>
<nav aria-label="둘러보기">{navh}</nav></header></div>
<main class="wrap">{body}<div class="adfit" id="adfit" aria-label="광고"></div></main>
<div class="wrap"><footer><nav><a href="/area">지역별 이번 주말</a><a href="/festival">축제 달력</a><a href="/theme">테마 여행</a><a href="/">말로 찾기</a></nav>
<b>관광정보 출처</b> 한국관광공사 (TourAPI · 공공데이터포털). 장소 사진의 저작권은 한국관광공사와 원 저작자에게 있습니다.<br>
날씨는 기상청 단기·중기예보, 혼잡 예상은 한국관광공사 관광지 집중률 예측입니다. 운영시간·휴무는 방문 전에 꼭 확인해 주세요.<br>
만든 사람 9seebird</footer></div>
<script>{AD_JS}</script></body></html>"""


def html_response(s, status=200):
    return HTMLResponse(s, status_code=status, headers={"Cache-Control": "public, max-age=600"})


def card(r, sub="", badge="", fest=False):
    img = f'<img src="{e(https(r["image_url"]))}" alt="{e(r["title"])}" loading="lazy" onerror="this.remove()">' \
        if r.get("image_url") else ""
    b = f'<span class="b{" f" if fest else ""}">{e(badge)}</span>' if badge else ""
    return (f'<a class="card" href="/place/{r["content_id"]}"><div class="ph">{img}{b}</div>'
            f'<div class="t"><b>{e(r["title"])}</b><small>{e(sub)}</small></div></a>')


def where2(addr):
    return " ".join((addr or "").split(" ")[:2])


def grid(rows, fn):
    return f'<div class="grid">{"".join(fn(r) for r in rows)}</div>' if rows else '<p class="empty">아직 모인 곳이 없어요.</p>'


COLS = ("p.content_id, p.title, p.addr, p.image_url, p.area_code, p.content_type, p.lat, p.lon, "
        "p.event_start, p.event_end")


def cached(kind_key, build):
    """완성된 HTML 을 한 시간 캐시 (같은 시간대엔 DB 를 다시 안 읽는다)."""
    key = cache.make_key("page", k=kind_key, h=datetime.now(KST).strftime("%Y%m%d%H"))
    with connect() as conn:
        hit = cache.get(conn, "page", key)
        if hit:
            return hit
        out = build(conn)
        if out:
            cache.put(conn, "page", key, out)
        return out


def area_counts(conn):
    rows = conn.execute("SELECT area_code, count(*) n FROM places WHERE overview IS NOT NULL GROUP BY area_code").fetchall()
    return {r["area_code"]: r["n"] for r in rows}


# ── 지역 ─────────────────────────────────────────────────────────────────────
@router.get("/area")
def area_index():
    def build(conn):
        n = area_counts(conn)
        sat, sun = weekend()
        chips = "".join(f'<a class="chip" href="/area/{s}">{e(LONG[c])} <small style="color:var(--muted)">{n.get(c, 0):,}곳</small></a>'
                        for c, s in AREAS if n.get(c))
        body = f"""<div class="crumb"><a href="/">반나절</a> › 지역별</div>
<h1>지역별 이번 주말 갈 만한 곳</h1>
<p class="lead">{md(sat)}({DOW[sat.weekday()]}){'' if sat == sun else f'~{md(sun)}({DOW[sun.weekday()]})'} 주말 기준으로, 지역마다 날씨·열리는 축제·덜 붐빌 곳·실내 명소·맛집을 매일 새로 골라 둡니다.</p>
<div class="chips">{chips}</div>"""
        return page("지역별 이번 주말 갈 만한 곳", "서울·부산·제주 등 지역별로 이번 주말 날씨, 축제, 덜 붐비는 관광지, 실내 명소와 맛집을 매일 골라 드립니다.",
                    "/area", body, nav="area")
    return html_response(cached("area-index", build))


@router.get("/area/{slug}")
def area_page(slug: str):
    code = CODE.get(slug)
    if not code:
        raise HTTPException(404)

    def build(conn):
        name, long = AREA_NAME[code], LONG[code]
        t = today()
        sat, sun = weekend(t)
        p = {"a": code}
        fest = conn.execute(f"""SELECT {COLS} FROM places p WHERE p.area_code = %(a)s AND p.content_type = 15
            AND p.event_start IS NOT NULL AND p.event_start <= %(sun)s AND p.event_end >= %(t)s
            ORDER BY (p.image_url IS NULL), p.event_start, p.content_id LIMIT 8""", p | {"t": t, "sun": sun}).fetchall()
        try:
            calm = conn.execute(f"""WITH wk AS (SELECT c.content_id, avg(c.rate) rate FROM crowd_forecast c
                  WHERE c.content_id IS NOT NULL AND c.ymd BETWEEN %(sat)s AND %(sun)s GROUP BY c.content_id)
                SELECT {COLS}, wk.rate FROM wk JOIN places p ON p.content_id = wk.content_id
                WHERE p.area_code = %(a)s AND p.image_url IS NOT NULL AND wk.rate < 50
                ORDER BY {shuffle_sql()} LIMIT 8""", p | {"sat": sat, "sun": sun}).fetchall()
        except Exception:
            calm = []
        pick = lambda cond, n=8: conn.execute(f"""SELECT {COLS} FROM places p WHERE p.area_code = %(a)s
            AND p.image_url IS NOT NULL AND p.overview IS NOT NULL AND {cond}
            ORDER BY {shuffle_sql()} LIMIT {n}""", p).fetchall()
        indoor = pick("p.indoor IS TRUE AND p.content_type IN (12, 14)")
        sights = pick("p.content_type = 12 AND p.indoor IS NOT TRUE")
        food = pick("p.content_type = 39")
        if not (fest or calm or indoor or sights or food):
            return None
        # 주말 날씨
        wx_html = ""
        try:
            w = weather.week(conn, *CENTER[code])
            days = {d["date"]: d for d in (w.get("days") or [])}
            cells = []
            for d in sorted({sat, sun}):
                x = days.get(d.isoformat())
                if not x:
                    continue
                am, pm = x.get("am") or x.get("pm"), x.get("pm") or x.get("am")
                wf = (am or {}).get("wf") or ""
                pop = max(int((am or {}).get("pop") or 0), int((pm or {}).get("pop") or 0))
                tmp = f'{x["tmin"]}° / {x["tmax"]}°' if x.get("tmin") is not None else ""
                desc = wf if (am or {}).get("wf") == (pm or {}).get("wf") else f'{wf} → {(pm or {}).get("wf") or ""}'
                cells.append(f'<div><i>{icon(desc)}</i><span><b>{md(d)}({DOW[d.weekday()]}) {e(desc)}</b>'
                             f'<small>{tmp}{" · " if tmp else ""}비 {pop}%</small></span></div>')
            if cells:
                wx_html = f'<h2>주말 날씨 <small>기상청 예보 · {e(name)} 기준</small></h2><div class="wx">{"".join(cells)}</div>'
        except Exception:
            pass
        has = area_counts(conn)
        others = "".join(f'<a class="chip" href="/area/{s}">{e(AREA_NAME[c])}</a>' for c, s in AREAS if c != code and has.get(c))
        themes = "".join(f'<a class="chip" href="/theme/{x["slug"]}">{e(x["short"])}</a>' for x in THEMES)
        span = f"{md(sat)}({DOW[sat.weekday()]})" + ("" if sat == sun else f"~{md(sun)}({DOW[sun.weekday()]})")
        sec = []
        if fest:
            sec.append(f'<h2>이번 주말 열리는 축제 <small>{span}</small></h2>' + grid(fest, lambda r: card(
                r, where2(r["addr"]), f'{md(r["event_start"])}~{md(r["event_end"])}', True)))
        if calm:
            sec.append(f'<h2>이번 주말 덜 붐빌 곳 <small>관광지 집중률 예측</small></h2>' + grid(calm, lambda r: card(
                r, where2(r["addr"]), "여유 예상")))
        if indoor:
            sec.append('<h2>실내 명소 <small>비 오거나 추운 날</small></h2>' + grid(indoor, lambda r: card(r, where2(r["addr"]))))
        if sights:
            sec.append('<h2>가볼 만한 관광지</h2>' + grid(sights, lambda r: card(r, where2(r["addr"]))))
        if food:
            sec.append('<h2>맛집 <small>한국관광공사 소개</small></h2>' + grid(food, lambda r: card(r, where2(r["addr"]))))
        body = f"""<div class="crumb"><a href="/">반나절</a> › <a href="/area">지역별</a> › {e(name)}</div>
<h1>{e(long)} 이번 주말 갈 만한 곳</h1>
<p class="lead">{span} 주말에 {e(long)}에서 가기 좋은 곳을 골랐어요. 열리는 축제, 덜 붐빌 것으로 예상되는 관광지, 날씨와 상관없는 실내 명소, 맛집까지 매일 새로 바뀝니다.</p>
<div class="acts"><a class="cta" href="/?area={code}">{e(name)}에서 말로 찾기</a><a class="btn" href="/festival">축제 달력</a></div>
{wx_html}{''.join(sec)}
<h2>주제로 찾기</h2><div class="chips">{themes}</div>
<h2>다른 지역</h2><div class="chips">{others}</div>"""
        ld = [{"@context": "https://schema.org", "@type": "ItemList", "name": f"{long} 이번 주말 갈 만한 곳",
               "itemListElement": [{"@type": "ListItem", "position": i + 1, "url": f"{SITE}/place/{r['content_id']}",
                                    "name": r["title"]} for i, r in enumerate([*fest, *calm, *indoor, *sights][:20])]},
              crumbs([("반나절", "/"), ("지역별", "/area"), (name, f"/area/{slug}")])]
        img = next((r["image_url"] for r in [*fest, *calm, *indoor, *sights] if r.get("image_url")), None)
        return page(f"{long} 이번 주말 갈 만한 곳 ({span})",
                    f"{span} {long} 주말 여행: 날씨, 이번 주 축제, 덜 붐비는 관광지, 실내 명소와 맛집을 매일 새로 골라 드립니다.",
                    f"/area/{slug}", body, jsonld=ld, image=img, nav="area")

    out = cached(f"area-{slug}", build)
    if not out:
        raise HTTPException(404)
    return html_response(out)


def icon(wf):
    wf = wf or ""
    if "눈" in wf:
        return "❄️"
    if "소나기" in wf:
        return "🌦️"
    if "비" in wf:
        return "🌧️"
    if "흐림" in wf or "흐리고" in wf:
        return "☁️"
    if "구름" in wf:
        return "⛅"
    return "☀️" if wf else "🌤️"


def crumbs(items):
    return {"@context": "https://schema.org", "@type": "BreadcrumbList",
            "itemListElement": [{"@type": "ListItem", "position": i + 1, "name": n, "item": SITE + u}
                                for i, (n, u) in enumerate(items)]}


# ── 축제 달력 ────────────────────────────────────────────────────────────────
@router.get("/festival")
def festival_now():
    return RedirectResponse(f"/festival/{today():%Y-%m}", status_code=302)


@router.get("/festival/{ym}")
def festival_month(ym: str):
    m = re.fullmatch(r"(20\d\d)-(0[1-9]|1[0-2])", ym)
    if not m:
        raise HTTPException(404)
    y, mo = int(m[1]), int(m[2])
    start = date(y, mo, 1)
    end = (date(y + (mo == 12), mo % 12 + 1, 1)) - timedelta(days=1)
    t = today()
    if abs((start.year - t.year) * 12 + start.month - t.month) > 14:
        raise HTTPException(404)

    def build(conn):
        rows = conn.execute(f"""SELECT {COLS} FROM places p WHERE p.content_type = 15 AND p.event_start IS NOT NULL
            AND p.event_start <= %(e)s AND p.event_end >= %(s)s
            ORDER BY p.event_start, (p.image_url IS NULL), p.content_id LIMIT 600""", {"s": start, "e": end}).fetchall()
        by = {}
        for r in rows:
            by.setdefault(r["area_code"], []).append(r)
        prev_m = (start - timedelta(days=1)).strftime("%Y-%m")
        next_m = (end + timedelta(days=1)).strftime("%Y-%m")
        jump = "".join(f'<a class="chip" href="#{SLUG[c]}">{e(AREA_NAME[c])} {len(by[c])}</a>' for c, _ in AREAS if c in by)

        def fcard(r):
            on = r["event_start"] <= t <= r["event_end"]
            badge = "진행 중" if on else (f'{md(r["event_start"])} 시작' if r["event_start"] > t else "끝남")
            if r["event_end"] < t:
                badge = "끝남"
            return card(r, f'{md(r["event_start"])}~{md(r["event_end"])} · {where2(r["addr"])}', badge, fest=True)
        secs = "".join(f'<h2 id="{SLUG[c]}">{e(LONG[c])} <small>{len(by[c])}개</small></h2>' + grid(by[c], fcard)
                       for c, _ in AREAS if c in by)
        body = f"""<div class="crumb"><a href="/">반나절</a> › 축제 달력 › {y}년 {mo}월</div>
<h1>{y}년 {mo}월 전국 축제 일정</h1>
<p class="lead">{y}년 {mo}월에 열리는 축제·행사 {len(rows)}개를 지역별로 모았습니다. 한국관광공사 행사 정보 기준이며, 일정은 바뀔 수 있으니 가기 전에 주최 측 공지를 꼭 확인하세요.</p>
<div class="mon"><a class="btn" href="/festival/{prev_m}">← {int(prev_m[5:])}월</a><a class="btn" href="/festival/{next_m}">{int(next_m[5:])}월 →</a></div>
<div class="chips" style="margin-top:10px">{jump}</div>
{secs or '<p class="empty">이달 축제 정보가 아직 없어요.</p>'}"""
        ld = [{"@context": "https://schema.org", "@type": "Event", "name": r["title"],
               "startDate": r["event_start"].isoformat(), "endDate": r["event_end"].isoformat(),
               "eventAttendanceMode": "https://schema.org/OfflineEventAttendanceMode",
               "location": {"@type": "Place", "name": r["title"], "address": r["addr"] or ""},
               **({"image": https(r["image_url"])} if r.get("image_url") else {}),
               "url": f"{SITE}/place/{r['content_id']}"} for r in rows[:30]]
        ld.append(crumbs([("반나절", "/"), ("축제 달력", "/festival"), (f"{y}년 {mo}월", f"/festival/{ym}")]))
        return page(f"{y}년 {mo}월 축제 일정 (전국 {len(rows)}개)",
                    f"{y}년 {mo}월 전국 축제·행사 일정을 지역별로 정리했습니다. 서울·경기·강원·부산·제주 등 {len(rows)}개 축제의 기간과 장소.",
                    f"/festival/{ym}", body, jsonld=ld,
                    image=next((r["image_url"] for r in rows if r.get("image_url")), None), nav="festival")
    return html_response(cached(f"fest-{ym}", build))


# ── 테마 ─────────────────────────────────────────────────────────────────────
@router.get("/theme")
def theme_index():
    chips = "".join(f'<a class="card" href="/theme/{t["slug"]}" style="padding:16px 18px"><b>{e(t["title"])}</b>'
                    f'<small style="white-space:normal;margin-top:4px">{e(t["intro"][:60])}…</small></a>' for t in THEMES)
    body = f"""<div class="crumb"><a href="/">반나절</a> › 테마 여행</div>
<h1>테마로 찾는 국내 여행지</h1>
<p class="lead">비 오는 날 실내, 아이랑, 반려견과, 단풍·야경 명소처럼 상황과 주제에 맞는 곳을 모았습니다. 목록은 매일 새로 섞여요.</p>
<div class="grid">{chips}</div>"""
    return html_response(page("테마로 찾는 국내 여행지", "비 오는 날 실내 명소, 아이랑 가기 좋은 곳, 반려견 동반 여행지, 단풍·야경 명소, 바다 보이는 카페까지 주제별 국내 여행지 모음.",
                              "/theme", body, nav="theme"))


@router.get("/theme/{slug}")
def theme_page(slug: str):
    th = THEME.get(slug)
    if not th:
        raise HTTPException(404)

    def build(conn):
        rows = conn.execute(f"""SELECT {COLS} FROM places p WHERE p.image_url IS NOT NULL AND p.overview IS NOT NULL
            AND ({th["where"]}) ORDER BY {shuffle_sql()} LIMIT 40""").fetchall()
        total = conn.execute(f"SELECT count(*) n FROM places p WHERE ({th['where']})").fetchone()["n"]
        others = "".join(f'<a class="chip{" on" if x["slug"] == slug else ""}" href="/theme/{x["slug"]}">{e(x["short"])}</a>' for x in THEMES)
        body = f"""<div class="crumb"><a href="/">반나절</a> › <a href="/theme">테마 여행</a> › {e(th["short"])}</div>
<h1>{e(th["title"])}</h1>
<p class="lead">{e(th["intro"])} 전국 {total:,}곳 가운데 {len(rows)}곳을 골랐고, 목록은 매일 바뀝니다.</p>
<div class="acts"><a class="cta" href="/?q={quote(th["q"])}">“{e(th["q"])}” 말로 찾기</a></div>
{grid(rows, lambda r: card(r, f'{AREA_NAME.get(r["area_code"], "")} · {TYPE.get(r["content_type"], "")}'))}
<h2>다른 주제</h2><div class="chips">{others}</div>"""
        ld = [{"@context": "https://schema.org", "@type": "ItemList", "name": th["title"],
               "itemListElement": [{"@type": "ListItem", "position": i + 1, "url": f"{SITE}/place/{r['content_id']}",
                                    "name": r["title"]} for i, r in enumerate(rows[:20])]},
              crumbs([("반나절", "/"), ("테마 여행", "/theme"), (th["short"], f"/theme/{slug}")])]
        return page(f"{th['title']} 추천 {len(rows)}곳", th["intro"], f"/theme/{slug}", body, jsonld=ld,
                    image=rows[0]["image_url"] if rows else None, nav="theme")
    return html_response(cached(f"theme-{slug}", build))


# ── 장소 ─────────────────────────────────────────────────────────────────────
@router.get("/place/{cid}")
def place_page(cid: int):
    def build(conn):
        r = conn.execute("SELECT * FROM places WHERE content_id = %s", (cid,)).fetchone()
        if not r:
            return None
        r = dict(r)
        name = AREA_NAME.get(r["area_code"], "")
        slug = SLUG.get(r["area_code"])
        typ = TYPE.get(r["content_type"], "")
        ov = clean(r.get("overview"))
        t = today()
        facts = []
        if r.get("event_start"):
            facts.append(("기간", f'{r["event_start"]:%Y.%m.%d} ~ {r["event_end"]:%Y.%m.%d}'))
        for k, lab in [("usetime", "운영시간"), ("restdate", "쉬는 날"), ("usefee", "요금"), ("menu", "대표 메뉴"),
                       ("parking", "주차")]:
            v = clean(r.get(k))
            if v:
                facts.append((lab, v))
        tel = clean(r.get("tel") or r.get("infocenter"))
        if tel:
            facts.append(("문의", tel))
        addr = " ".join(x for x in [r.get("addr"), r.get("addr_detail")] if x)
        if addr:
            facts.append(("주소", addr))
        flags = []
        if r.get("indoor") is True:
            flags.append("실내")
        if r.get("pet_allowed") is True:
            flags.append("반려동물 동반")
        if r.get("baby_ok") is True or r.get("stroller_ok") is True:
            flags.append("유모차")
        if r.get("wheelchair_ok") is True:
            flags.append("휠체어")
        near = []
        if r.get("lat"):
            near = conn.execute(f"""SELECT {COLS} FROM places p WHERE p.content_id <> %(id)s AND p.image_url IS NOT NULL
                AND p.overview IS NOT NULL AND p.content_type <> 15
                AND abs(p.lat - %(lat)s) < 0.05 AND abs(p.lon - %(lon)s) < 0.06
                ORDER BY (p.lat - %(lat)s)^2 + ((p.lon - %(lon)s) * 0.8)^2 LIMIT 8""",
                                {"id": cid, "lat": r["lat"], "lon": r["lon"]}).fetchall()
        img = r.get("image_url")
        hero = f'<div class="hero"><img src="{e(https(img))}" alt="{e(r["title"])}" onerror="this.remove()"></div>' if img else ""
        mapl = (f'<a class="btn" target="_blank" rel="noopener" href="https://map.kakao.com/link/map/'
                f'{e(r["title"])},{r["lat"]},{r["lon"]}">카카오맵</a>') if r.get("lat") else ""
        course = "" if r["content_type"] == 15 else f'<a class="cta" href="/?course={cid}">여기서 반나절 코스 짜기</a>'
        status = ""
        if r.get("event_start"):
            status = ("진행 중" if r["event_start"] <= t <= r["event_end"] else
                      "곧 열려요" if r["event_start"] > t else "끝난 행사")
        tags = "".join(f'<span class="tag">{e(x)}</span>' for x in [typ, status, *flags] if x)
        body = f"""<div class="crumb"><a href="/">반나절</a> › {f'<a href="/area/{slug}">{e(name)}</a> › ' if slug else ''}{e(r["title"])}</div>
<div>{tags}</div><h1>{e(r["title"])}</h1>
<p class="lead" style="margin-bottom:6px">{e(addr)}</p>
{hero}
<div class="cols"><div>{f'<div class="ov">{e(ov)}</div>' if ov else '<p class="empty">소개글이 아직 없어요.</p>'}
<div class="acts">{course}{mapl}{f'<a class="btn" href="/area/{slug}">{e(name)} 이번 주말</a>' if slug else ''}</div></div>
<div class="box"><dl class="facts">{''.join(f'<dt>{k}</dt><dd>{e(v)}</dd>' for k, v in facts) or '<dd>정보가 아직 없어요.</dd>'}</dl>
<p style="font-size:12px;color:var(--muted);margin:12px 0 0">한국관광공사 정보 기준이에요. 운영시간·요금은 바뀔 수 있으니 방문 전에 확인하세요.</p></div></div>
{f'<h2>근처 가볼 만한 곳 <small>{e(r["title"])} 주변</small></h2>' + grid(near, lambda x: card(x, TYPE.get(x["content_type"], ""))) if near else ''}"""
        ld_type = {12: "TouristAttraction", 14: "Museum", 15: "Event", 28: "SportsActivityLocation",
                   39: "Restaurant"}.get(r["content_type"], "Place")
        ld = {"@context": "https://schema.org", "@type": ld_type, "name": r["title"], "url": f"{SITE}/place/{cid}",
              "description": ov[:300]}
        if img:
            ld["image"] = https(img)
        if ld_type == "Event":
            ld.update({"startDate": r["event_start"].isoformat() if r.get("event_start") else None,
                       "endDate": r["event_end"].isoformat() if r.get("event_end") else None,
                       "eventAttendanceMode": "https://schema.org/OfflineEventAttendanceMode",
                       "location": {"@type": "Place", "name": r["title"], "address": addr}})
        else:
            ld["address"] = addr
            if r.get("lat"):
                ld["geo"] = {"@type": "GeoCoordinates", "latitude": r["lat"], "longitude": r["lon"]}
        if tel and ld_type != "Event":
            ld["telephone"] = tel.split("\n")[0][:40]
        cl = [("반나절", "/")] + ([(name, f"/area/{slug}")] if slug else []) + [(r["title"], f"/place/{cid}")]
        desc = re.sub(r"\s+", " ", ov)[:140] or f"{name} {typ} {r['title']} 정보"
        title = f"{r['title']} — {name} {typ}" if name else r["title"]
        return page(title, desc, f"/place/{cid}", body, jsonld=[ld, crumbs(cl)], image=img,
                    noindex=not ov)
    out = cached(f"place-{cid}", build)
    if not out:
        return html_response(page("찾는 장소가 없어요", "없는 장소입니다.", f"/place/{cid}",
                                  '<h1>찾는 장소가 없어요</h1><p class="lead">정보가 내려갔거나 주소가 잘못됐어요.</p>'
                                  '<a class="cta" href="/">반나절 첫 화면으로</a>', noindex=True), 404)
    return html_response(out)


# ── sitemap ──────────────────────────────────────────────────────────────────
@router.get("/sitemap.xml")
def sitemap():
    def build(conn):
        t = today()
        urls = [("/", "daily", "1.0"), ("/area", "daily", "0.8"), ("/festival", "daily", "0.8"),
                ("/theme", "weekly", "0.7"), ("/lab", "monthly", "0.3")]
        n = area_counts(conn)
        urls += [(f"/area/{s}", "daily", "0.8") for c, s in AREAS if n.get(c)]
        for k in range(-1, 3):
            mth = (t.replace(day=1) + timedelta(days=32 * k)).replace(day=1) if k >= 0 else \
                (t.replace(day=1) - timedelta(days=1)).replace(day=1)
            urls.append((f"/festival/{mth:%Y-%m}", "daily", "0.7"))
        urls += [(f"/theme/{x['slug']}", "daily", "0.7") for x in THEMES]
        rows = conn.execute("""SELECT content_id FROM places WHERE overview IS NOT NULL AND overview <> ''
            AND (content_type <> 15 OR event_end >= %s) ORDER BY content_id LIMIT 45000""",
                            (t - timedelta(days=60),)).fetchall()
        urls += [(f"/place/{r['content_id']}", "weekly", "0.5") for r in rows]
        seen, body = set(), []
        for u, f, pr in urls:
            if u in seen:
                continue
            seen.add(u)
            body.append(f"<url><loc>{SITE}{u}</loc><changefreq>{f}</changefreq><priority>{pr}</priority></url>")
        return ('<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                + "".join(body) + "</urlset>")
    return Response(cached("sitemap", build), media_type="application/xml")
