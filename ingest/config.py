import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

TOUR_API_KEY  = os.getenv("TOUR_API_KEY", "")
TOUR_BASE     = os.getenv("TOUR_BASE", "https://apis.data.go.kr/B551011/KorService2").rstrip("/")
TOUR_APP_NAME = os.getenv("TOUR_APP_NAME", "triprag")
TOUR_DAILY_LIMIT = int(os.getenv("TOUR_DAILY_LIMIT", "1000"))

# 모델 제공처: bizrouter | gemini  (gemini 는 구글 AI Studio 키 하나로 임베딩·생성을 쓴다)
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "bizrouter").strip().lower()

BIZROUTER_API_KEY = os.getenv("BIZROUTER_API_KEY", "")
BIZROUTER_BASE    = os.getenv("BIZROUTER_BASE", "https://api.bizrouter.ai/v1").rstrip("/")
GEMINI_API_KEY    = os.getenv("GEMINI_API_KEY", "")
GEMINI_BASE       = os.getenv("GEMINI_BASE", "https://generativelanguage.googleapis.com/v1beta").rstrip("/")

_DEFAULTS = {
    "bizrouter": dict(embed="bizrouter/qwen3-embedding-4b", rerank="qwen/qwen3-reranker-8b",
                      chat="google/gemini-3.5-flash-lite"),
    # Gemini 에는 리랭커 API 가 없다 → 생성 모델이 후보를 읽고 줄세운다 (bizrouter.rerank 참고)
    "gemini":    dict(embed="gemini-embedding-001", rerank="gemini-3.5-flash-lite",
                      chat="gemini-3.5-flash-lite"),
}[LLM_PROVIDER]
# .env 에 'EMBED_MODEL=' 처럼 비워 두면 제공처 기본값
EMBED_MODEL  = os.getenv("EMBED_MODEL") or _DEFAULTS["embed"]
EMBED_DIM    = int(os.getenv("EMBED_DIM") or "1024")
RERANK_MODEL = os.getenv("RERANK_MODEL") or _DEFAULTS["rerank"]
CHAT_MODEL   = os.getenv("CHAT_MODEL") or _DEFAULTS["chat"]

DB = dict(
    host=os.getenv("DB_HOST", "localhost"),
    port=int(os.getenv("DB_PORT", "5433")) if os.getenv("DB_HOST", "localhost") != "db" else 5432,
    dbname=os.getenv("POSTGRES_DB", "triprag"),
    user=os.getenv("POSTGRES_USER", "triprag"),
    password=os.getenv("POSTGRES_PASSWORD", "triprag"),
)

AREA_CODE = int(os.getenv("AREA_CODE", "39"))          # 39 = 제주

# contentTypeId → (이름, 상세수집 우선순위). 낮을수록 먼저 가져온다.
#   검색 데모의 주인공인 관광지·문화시설이 1순위.
#   음식점은 코스 짜기에 이름·좌표·분류만 있으면 되므로 소개글은 맨 뒤로 미룬다.
CONTENT_TYPES = {
    12: ("관광지",     1),
    14: ("문화시설",   1),
    28: ("레포츠",     2),
    15: ("축제공연행사", 3),
    39: ("음식점",     5),
}
