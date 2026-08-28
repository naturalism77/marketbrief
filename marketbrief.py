"""
MarketBrief — Daily US Market Brief Generator
실제 작동 버전: yfinance + Anthropic Claude API

사용법:
    python marketbrief.py              # 시황 생성 후 output/ 폴더에 저장
    python marketbrief.py --dry-run    # API 미호출, 데이터 수집만 확인
    python marketbrief.py --show-data  # 수집된 raw 데이터 출력 후 종료

호출 타이밍: 매일 06:00~07:00 KST (미국 증시 마감 후 충분히 지난 시점)
"""

import io
import os
import re
import csv
import sys
import json
import math
import logging
import datetime
import argparse
from pathlib import Path

import pytz
import requests
import yfinance as yf
import feedparser
from dotenv import load_dotenv
from x_topics import (
    fetch_x_topics, save_x_topics,
    load_latest_x_topics, render_x_topics_section,
)

load_dotenv()

# ============================================================
# 0. ENVIRONMENT & LOGGING SETUP
# ============================================================

GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY", "")
KST = pytz.timezone("Asia/Seoul")


def setup_logging(log_to_file: bool = False) -> logging.Logger:
    """로거 초기화. log_to_file=True 시 logs/ 폴더에도 저장."""
    log_dir = Path("logs")
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]

    if log_to_file:
        log_dir.mkdir(exist_ok=True)
        date_str  = datetime.datetime.now(KST).strftime("%Y%m%d")
        file_path = log_dir / f"marketbrief_{date_str}.log"
        handlers.append(logging.FileHandler(file_path, encoding="utf-8"))

    logging.basicConfig(
        level   = logging.INFO,
        format  = "%(asctime)s  %(message)s",
        datefmt = "%H:%M:%S",
        handlers= handlers,
    )
    return logging.getLogger("marketbrief")


# ============================================================
# 1. CONFIGURATION
# ============================================================

def load_config() -> dict:
    """config.json 로드. 파일 없으면 기본값 반환."""
    config_path = Path(__file__).parent / "config.json"
    defaults = {
        "model": "claude-opus-4-6",
        "max_retries": 3,
        "max_tokens": 2048,
        "data": {"etf_count": 20, "news_count": 5, "news_rss_feeds": []},
        "hot_topics_watchlist": [],
        "corp_news_max_age_days": 3,
        "output": {
            "save_markdown": True, "save_html": True,
            "save_json": True, "save_push_payload": True,
            "open_browser_after_run": False, "auto_update_index": True,
        },
        "notifications": {"email_enabled": False, "slack_enabled": False},
        "schedule": {"run_at_kst": "06:00", "push_send_at_kst": "06:00"},
    }
    if not config_path.exists():
        return defaults
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
        # 최상위 키만 병합 (중첩 dict는 config.json 우선)
        return {**defaults, **{k: v for k, v in raw.items() if not k.startswith("_")}}
    except Exception as e:
        print(f"⚠️  config.json 파싱 실패 ({e}) — 기본값 사용")
        return defaults


CFG = load_config()
GEMINI_MODEL = CFG.get("model", "gemini-2.5-flash")

INDEX_TICKERS = {
    "SP500":  "^GSPC",
    "NASDAQ": "^IXIC",
    "DOW":    "^DJI",
    "VIX":    "^VIX",
}

# 미국 국채 금리 (yfinance)
YIELD_TICKERS = {
    "UST_2Y":  "^FVX",   # 5년물 (2년물 ^IRX는 T-bill, ^FVX가 더 대표적)
    "UST_10Y": "^TNX",   # 10년물
    "UST_30Y": "^TYX",   # 30년물
}

# 글로벌 매크로 지표
MACRO_TICKERS = {
    "DXY":  "DX-Y.NYB",  # 달러 인덱스
    "Gold": "GC=F",       # 금 선물 ($/oz)
    "Oil":  "CL=F",       # WTI 원유 선물 ($/bbl)
}

# 원자재 선물 (원자재 성과표)
COMMODITY_TICKERS = {
    "금":       ("GC=F",   "🥇"),
    "은":       ("SI=F",   "🥈"),
    "구리":     ("HG=F",   "🇺🇸"),
    "백금":     ("PL=F",   "⬜"),
    "브렌트유": ("BZ=F",   "🇬🇧"),
    "WTI유":   ("CL=F",   "🇺🇸"),
    "천연가스": ("NG=F",   "🇺🇸"),
    "난방유":   ("HO=F",   "🇺🇸"),
    "커피 C":  ("KC=F",   "🇺🇸"),
    "옥수수":   ("ZC=F",   "🇺🇸"),
    "소맥":     ("ZW=F",   "🇺🇸"),
    "대두":     ("ZS=F",   "🇺🇸"),
}

# CNN Fear & Greed Index API
FEAR_GREED_URL = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"

SECTOR_ETFS = {
    "XLK":  "Technology",
    "XLV":  "Health Care",
    "XLE":  "Energy",
    "XLF":  "Financials",
    "XLI":  "Industrials",
    "XLU":  "Utilities",
    "XLP":  "Consumer Staples",
    "XLRE": "Real Estate",
    "XLB":  "Materials",
    "XLC":  "Communication",
    "XLY":  "Consumer Disc.",
}

# LeverageShares ETF → 기초자산 매핑 (fallback용 하드코딩)
_LS_ETF_TICKERS_FALLBACK = [
    ("NVDG", "2x Long NVDA Daily ETF", "2x"),
    ("TSLG", "2x Long TSLA Daily ETF", "2x"),
    ("ARMG", "2x Long ARM Daily ETF", "2x"),
    ("ASMG", "2x Long ASML Daily ETF", "2x"),
    ("TSMG", "2x Long TSM Daily ETF", "2x"),
    ("AMDG", "2x Long AMD Daily ETF", "2x"),
    ("COIG", "2x Long COIN Daily ETF", "2x"),
    ("HOOG", "2x Long HOOD Daily ETF", "2x"),
    ("PANG", "2x Long PANW Daily ETF", "2x"),
    ("ADBG", "2x Long ADBE Daily ETF", "2x"),
    ("PYPG", "2x Long PYPL Daily ETF", "2x"),
    ("XYZG", "2x Long XYZ Daily ETF", "2x"),
    ("CRMG", "2x Long CRM Daily ETF", "2x"),
    ("PLTG", "2x Long PLTR Daily ETF", "2x"),
    ("AVGG", "2x Long AVGO Daily ETF", "2x"),
    ("RTXG", "2x Long RTX Daily ETF", "2x"),
    ("BOEG", "2x Long BA Daily ETF", "2x"),
    ("AALG", "2x Long AAL Daily ETF", "2x"),
    ("UNHG", "2x Long UNH Daily ETF", "2x"),
    ("CRCG", "2x Long CRCL Daily ETF", "2x"),
    ("CRWG", "2x Long CRWV Daily ETF", "2x"),
    ("BULG", "2x Long BULL Daily ETF", "2x"),
    ("BAIG", "2x Long BBAI Daily ETF", "2x"),
    ("GLGG", "2x Long GLXY Daily ETF", "2x"),
    ("COTG", "2x Long COST Daily ETF", "2x"),
    ("FIGG", "2x Long FIG Daily ETF", "2x"),
    ("FUTG", "2x Long FUTU Daily ETF", "2x"),
    ("BLSG", "2x Long BLSH Daily ETF", "2x"),
    ("BMNG", "2x Long BMNR Daily ETF", "2x"),
    ("MPG",  "2x Long MP Daily ETF", "2x"),
    ("NBIG", "2x Long NBIS Daily ETF", "2x"),
    ("GEMG", "2x Long GEMI Daily ETF", "2x"),
    ("OSCG", "2x Long OSCR Daily ETF", "2x"),
    ("LULG", "2x Long LULU Daily ETF", "2x"),
    ("NUG",  "2x Long NU Daily ETF", "2x"),
    ("NETG", "2x Long NET Daily ETF", "2x"),
    ("NEMG", "2x Long NEM Daily ETF", "2x"),
    ("OKTG", "2x Long OKTA Daily ETF", "2x"),
    ("TERG", "2x Long TER Daily ETF", "2x"),
    ("ABNG", "2x Long ABNB Daily ETF", "2x"),
    ("SBU",  "2x Long SBUX Daily ETF", "2x"),
    ("CMGG", "2x Long CMG Daily ETF", "2x"),
    ("SPOG", "2x Long SPOT Daily ETF", "2x"),
    ("CIFG", "2x Long CIFR Daily ETF", "2x"),
    ("DUOG", "2x Long DUOL Daily ETF", "2x"),
    ("GRAG", "2x Long GRAB Daily ETF", "2x"),
    ("LACG", "2x Long LAC Daily ETF", "2x"),
    ("OPEG", "2x Long OPEN Daily ETF", "2x"),
    ("UPSG", "2x Long UPS Daily ETF", "2x"),
    ("IREG", "2x Long IREN Daily ETF", "2x"),
    ("BEG",  "2x Long BE Daily ETF", "2x"),
    ("GEVG", "2x Long GEV Daily ETF", "2x"),
    ("SATG", "2x Long SATS Daily ETF", "2x"),
    ("NIOG", "2x Long NIO Daily ETF", "2x"),
    ("SNAG", "2x Long SNAP Daily ETF", "2x"),
    ("BIDG", "2x Long BIDU Daily ETF", "2x"),
    ("CNCG", "2x Long CNC Daily ETF", "2x"),
    ("KLAG", "2x Long KLAC Daily ETF", "2x"),
    ("PBRG", "2x Long PBR Daily ETF", "2x"),
    ("VALG", "2x Long VALE Daily ETF", "2x"),
    ("USGG", "2x Long USAR Daily ETF", "2x"),
    ("ONDG", "2x Long ONDS Daily ETF", "2x"),
    ("PLUL", "2x Long PLUG Daily ETF", "2x"),
    ("ALBG", "2x Long ALB Daily ETF", "2x"),
    ("HUTG", "2x Long HUT Daily ETF", "2x"),
    ("UUUG", "2x Long UUUU Daily ETF", "2x"),
    ("XPEG", "2x Long XPEV Daily ETF", "2x"),
    ("ORLG", "2x Long ORLY Daily ETF", "2x"),
    ("CRMU", "2x Long CRML Daily ETF", "2x"),
    ("UECG", "2x Long UEC Daily ETF", "2x"),
    ("DNNG", "2x Long DNN Daily ETF", "2x"),
    ("AXPG", "2x Long AXP Daily ETF", "2x"),
    ("FCXG", "2x Long FCX Daily ETF", "2x"),
    ("WLDU", "2x Long World Daily ETF", "2x"),
    ("GLWG", "2x Long GLW Daily ETF", "2x"),
    ("AAOG", "2x Long AAOI Daily ETF", "2x"),
    ("AMAU", "2x Long AMAT Daily ETF", "2x"),
    ("CATG", "2x Long CAT Daily ETF", "2x"),
    ("CIEG", "2x Long CIEN Daily ETF", "2x"),
    ("COHH", "2x Long COHR Daily ETF", "2x"),
    ("ETNG", "2x Long ETN Daily ETF", "2x"),
    ("HONG", "2x Long HON Daily ETF", "2x"),
    ("SNDG", "2x Long SNDK Daily ETF", "2x"),
    ("STXU", "2x Long STX Daily ETF", "2x"),
    ("CBRG", "2x Long CBRS Daily ETF", "2x"),
    ("NXPG", "2x Long NXPI Daily ETF", "2x"),
    ("ONG",  "2x Long ON Daily ETF", "2x"),
    ("SPCH", "2x Long SPCX Daily ETF", "2x"),
    ("SSPC", "2x Short SPCX ETF", "-2x"),
    ("ADIU", "2x Long ADI Daily ETF", "2x"),
    ("APHG", "2x Long APH Daily ETF", "2x"),
    ("AXTL", "2x Long AXTI Daily ETF", "2x"),
    ("FNG",  "2x Long FN Daily ETF", "2x"),
    ("KEYG", "2x Long KEYS Daily ETF", "2x"),
    ("MCHG", "2x Long MCHP Daily ETF", "2x"),
    ("TELG", "2x Long TEL Daily ETF", "2x"),
    ("TSEG", "2x Long TSEM Daily ETF", "2x"),
    ("AEHG", "2x Long AEHR Daily ETF", "2x"),
    ("ASTG", "2x Long ASTS Daily ETF", "2x"),
    ("CDNG", "2x Long CDNS Daily ETF", "2x"),
    ("ENTL", "2x Long ENTG Daily ETF", "2x"),
    ("FOMG", "2x Long FORM Daily ETF", "2x"),
    ("FPSX", "2x Long FPS Daily ETF", "2x"),
    ("GFSG", "2x Long GFS Daily ETF", "2x"),
    ("HPEL", "2x Long HPE Daily ETF", "2x"),
    ("MTSG", "2x Long MTSI Daily ETF", "2x"),
    ("SMTG", "2x Long SMTC Daily ETF", "2x"),
    ("AAPE", "2x Long AAPL Daily ETF", "2x"),
    ("AMZG", "2x Long AMZN Daily ETF", "2x"),
    ("GOOL", "2x Long GOOGL Daily ETF", "2x"),
    ("METG", "2x Long META Daily ETF", "2x"),
    ("JBLG", "2x Long JBL Daily ETF", "2x"),
    ("VIAG", "2x Long VIAV Daily ETF", "2x"),
    ("SKHZ", "1x Short SKHY Daily ETF", "-1x"),
    ("SKHX", "2x Long SKHY Daily ETF", "2x"),
]
_LS_PDF_URL = "https://leverageshares.com/us/storage/all-products-pdf/all-products.pdf"
_LS_CSV_PATH = Path(__file__).parent / "config" / "leverageshares_us_tickers.csv"


def _load_ls_etf_tickers_from_csv(csv_path: Path | None = None) -> list[tuple[str, str, str]]:
    """config/leverageshares_us_tickers.csv에서 LeverageShares 상장 ETF 전체를 읽는다.

    CSV 컬럼: BBG 티커, 영어 명칭, ISIN, CUSIP, SEDOLS, WKNs, RIC,
              Listing Date, Currency, Exchange, Management Fee
    - BOM 포함 UTF-8로 저장돼 있어 utf-8-sig로 읽는다.
    - BBG 티커가 비어있거나 "Website"인 행(빈 줄·푸터)은 스킵.
    - 영어 명칭 앞의 "Leverage Shares " 접두사는 표시용으로 제거.
    - 배율/방향은 이름에서 "Nx Long|Short" 패턴으로 추출(대소문자 무관).
      콤보 상품 등 패턴이 안 맞으면 기본값 "2x"로 채운다(표시용이라 랭킹에는 영향 없음).

    파일이 없거나 읽기 실패하면 빈 리스트 반환 — 호출부에서 PDF/하드코딩으로 대체.
    """
    path = csv_path or _LS_CSV_PATH
    if not path.exists():
        return []

    try:
        with path.open(encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))
    except Exception as exc:
        print(f"  [CSV 읽기 실패] {exc}")
        return []

    leverage_re = re.compile(r"(\d+)\s*[xX]\s+(Long|Short)", re.IGNORECASE)

    results: list[tuple[str, str, str]] = []
    for row in rows:
        ticker = (row.get("BBG 티커") or "").strip()
        if not ticker or ticker == "Website":
            continue

        raw_name = (row.get("영어 명칭") or "").strip()
        name = re.sub(r"^Leverage Shares\s+", "", raw_name)

        m = leverage_re.search(raw_name)
        if m:
            n, direction = m.group(1), m.group(2).lower()
            leverage = f"{n}x" if direction == "long" else f"-{n}x"
        else:
            leverage = "2x"

        results.append((ticker, name, leverage))

    return results


def _load_ls_etf_tickers_from_pdf() -> list[tuple[str, str, str]]:
    """LeverageShares All Products PDF에서 2x Long ETF 티커를 동적으로 파싱한다.

    PDF 구조:
      - "2x Daily Leveraged ETF" 헤더 페이지만 처리 (Capped Accelerated 등 제외)
      - 각 행: [LS코드] 0.75  [LS코드2] 0.75   (expense ratio = 0.75)
      - 다음 행: 2x Long XXX Daily ETP  2x Long YYY Daily ETP
      - NYSE Arca 티커 = LS코드 + "G"  (예: NVD → NVDG, COI → COIG)

    파싱 실패 시 빈 리스트를 반환하며, 호출부에서 fallback 처리한다.
    """
    try:
        import pdfplumber
    except ImportError:
        return []

    try:
        resp = requests.get(_LS_PDF_URL, timeout=30)
        resp.raise_for_status()
        pdf_bytes = io.BytesIO(resp.content)
    except Exception as exc:
        print(f"  [PDF 다운로드 실패] {exc}")
        return []

    # LS코드 + expense ratio 행 패턴: "NVD 0.75" 또는 "NVD 0.75  OKT 0.75"
    ticker_line_re = re.compile(
        r"^([A-Z]{2,6})\s+0\.75(?:\s+([A-Z]{2,6})\s+0\.75)?\s*$"
    )

    results: list[tuple[str, str, str]] = []
    seen: set[str] = set()

    try:
        with pdfplumber.open(pdf_bytes) as pdf:
            for page in pdf.pages:
                text = page.extract_text() or ""
                # "2x Daily Leveraged ETF" 페이지만 처리
                if "2x Daily Leveraged" not in text:
                    continue

                lines = [ln.strip() for ln in text.splitlines()]
                for i, line in enumerate(lines):
                    m = ticker_line_re.match(line)
                    if not m or i + 1 >= len(lines):
                        continue

                    next_line = lines[i + 1]
                    # 다음 줄을 "2x Long" 기준으로 분리해 개별 상품명 추출
                    name_parts = [
                        p.strip()
                        for p in re.split(r"(?=2x Long)", next_line)
                        if p.strip().startswith("2x Long")
                    ]

                    ls_codes = [g for g in [m.group(1), m.group(2)] if g]
                    for ls_code, raw_name in zip(ls_codes, name_parts):
                        nyse_ticker = ls_code + "G"
                        if nyse_ticker in seen:
                            continue
                        # pdfplumber가 "ETF" 끝을 "ET"로 잘라내는 경우 보정
                        name = raw_name if not raw_name.endswith(" ET") else raw_name + "F"
                        name = re.sub(r"\s+", " ", name).strip()
                        seen.add(nyse_ticker)
                        results.append((nyse_ticker, name, "2x"))
    except Exception as exc:
        print(f"  [PDF 파싱 실패] {exc}")
        return []

    return results


def _get_ls_etf_tickers() -> list[tuple[str, str, str]]:
    """LeverageShares 상장 ETF 티커 목록을 가져온다.

    우선순위: CSV(config/leverageshares_us_tickers.csv, 실제 소스) →
              PDF 동적 파싱(CSV 없거나 읽기 실패 시 대체) →
              하드코딩 fallback(둘 다 실패한 최후 안전장치).
    """
    print("  (LeverageShares CSV에서 ETF 티커 로딩 중...)")
    from_csv = _load_ls_etf_tickers_from_csv()
    if from_csv:
        print(f"  → CSV에서 {len(from_csv)}개 ETF 티커 로드 완료")
        return from_csv

    print("  → CSV 로드 실패, PDF에서 ETF 티커 로딩 중...")
    dynamic = _load_ls_etf_tickers_from_pdf()
    if dynamic:
        print(f"  → PDF에서 {len(dynamic)}개 2x Long ETF 티커 로드 완료")
        return dynamic

    print("  → PDF 로드도 실패, 하드코딩 fallback 사용")
    return _LS_ETF_TICKERS_FALLBACK


_UNDERLYING_FROM_NAME_RE = re.compile(r"\b(?:Long|Short)\s+([A-Z0-9]+)\b")


def _underlying_ticker(ticker: str, name: str) -> str:
    """ETF 상품명에서 실제 기초자산 티커를 추출한다.

    예: "2x Long NVDA Daily ETF" → "NVDA".
    기존에는 ETF 티커에서 "G"를 제거하는 방식(NVDG → NVD)을 썼는데, 이 방식은
    다수 상품에서 실제 기초자산과 다른 문자열을 만들어냈다(NVDG → NVD, 실제는 NVDA).
    상품명에 기초자산이 명시돼 있으므로 그걸 우선 사용하고, 콤보 상품처럼
    "Long"/"Short" 패턴이 없는 경우에만 기존 방식으로 대체한다.
    """
    m = _UNDERLYING_FROM_NAME_RE.search(name)
    if m:
        return m.group(1)
    return ticker.replace("G", "")


# CSV는 로컬 파일이라 PDF와 달리 모듈 로드 시점에 바로 읽어도 안전(네트워크 I/O 아님).
# CSV가 없거나 읽기 실패하면 하드코딩 fallback으로 대체.
_LS_ETF_TICKERS_FOR_UNDERLYING = _load_ls_etf_tickers_from_csv() or _LS_ETF_TICKERS_FALLBACK
ETF_UNDERLYING = {
    t: _underlying_ticker(t, name) for t, name, _ in _LS_ETF_TICKERS_FOR_UNDERLYING
}

NEWS_RSS_FEEDS = [
    "https://feeds.marketwatch.com/marketwatch/topstories/",
    "https://feeds.content.dowjones.io/public/rss/mw_realtimeheadlines",
    "https://feeds.reuters.com/reuters/businessNews",
    "https://rss.cnn.com/rss/money_markets.rss",
]

# 지정학적 이슈 전용 RSS
GEO_RSS_FEEDS = [
    "https://feeds.reuters.com/reuters/worldNews",
    "https://feeds.reuters.com/Reuters/worldNews",
    "https://rss.cnn.com/rss/cnn_world.rss",
    "https://feeds.bbci.co.uk/news/world/rss.xml",
]

# 기업 이슈 전용 RSS
CORP_RSS_FEEDS = [
    "https://feeds.bloomberg.com/markets/news.rss",
    "https://feeds.bloomberg.com/technology/news.rss",
    # fallback
    "https://feeds.marketwatch.com/marketwatch/marketpulse/",
    "https://feeds.reuters.com/reuters/companyNews",
    "https://rss.cnn.com/rss/money_companies.rss",
]

SYSTEM_PERSONA = """
당신은 20년 경력의 월스트리트 애널리스트입니다.
모든 출력은 반드시 한국어로 작성하세요. 수치·티커·고유명사는 영문 그대로 유지합니다.
문체는 간결하고 직관적이며, 바쁜 투자자가 5분 안에 핵심을 파악할 수 있어야 합니다.
영어 뉴스 헤드라인은 자연스러운 한국어로 번역하여 요약하세요.
불확실한 정보는 반드시 [확인 필요] 태그를 붙이세요.
"""

REQUIRED_SECTIONS = [
    "## 1. 주요 지수 요약",
    "## 2. 채권 시장",
    "## 3. 섹터 동향",
    "## 4. 오늘의 주요 이슈",
    "## 5. 애널리스트 총평",
    "## 6. Leverageshares x2 레버리지 ETF",
    "## 7. 경제 캘린더",
    "## 8. 실적발표 일정",
    "## 9. 지정학적 이슈",
    "## 10. 기업별 주요 이슈",
]


# ============================================================
# 2. MARKET DATA FETCHER
# ============================================================

class MarketDataFetcher:
    """yfinance 기반 시장 데이터 수집"""

    def fetch_indices(self) -> tuple:
        """주요 지수 종가·등락률 + 다중 기간 수익률 수집."""
        result = {}
        last_date = None
        today = datetime.datetime.now(KST)
        ytd_start = datetime.datetime(today.year, 1, 1, tzinfo=datetime.timezone.utc)

        def _pct(a, b):
            if b and b != 0:
                v = (a - b) / b * 100
                return f"{'+' if v >= 0 else ''}{v:.2f}%"
            return "-"

        for name, ticker in INDEX_TICKERS.items():
            try:
                hist = yf.Ticker(ticker).history(period="1y")
                hist = hist.dropna(subset=["Close"])
                if len(hist) < 2:
                    raise ValueError(f"데이터 부족: {ticker}")

                close = hist["Close"].iloc[-1]
                prev  = hist["Close"].iloc[-2]

                if last_date is None:
                    last_date = hist.index[-1].strftime("%Y-%m-%d")

                # 1주 전 (약 5거래일)
                w1 = hist["Close"].iloc[-6] if len(hist) >= 6 else None
                # 1개월 전 (약 21거래일)
                m1 = hist["Close"].iloc[-22] if len(hist) >= 22 else None
                # YTD: 연초에 가장 가까운 데이터
                ytd_idx = hist.index.searchsorted(ytd_start)
                ytd_price = hist["Close"].iloc[ytd_idx] if ytd_idx < len(hist) else None
                # 1년 전
                y1 = hist["Close"].iloc[0] if len(hist) >= 50 else None

                result[name] = {
                    "close":      f"{close:,.2f}",
                    "change_pct": _pct(close, prev),
                    "change_1w":  _pct(close, w1),
                    "change_1m":  _pct(close, m1),
                    "change_ytd": _pct(close, ytd_price),
                    "change_1y":  _pct(close, y1),
                }
            except Exception as e:
                print(f"  ⚠️  {ticker} 수집 실패: {e}")
                result[name] = {
                    "close": "[확인 필요]", "change_pct": "-",
                    "change_1w": "-", "change_1m": "-",
                    "change_ytd": "-", "change_1y": "-",
                }

        fallback_date = datetime.datetime.now(KST).strftime("%Y-%m-%d")
        return result, last_date or fallback_date

    def fetch_commodities(self) -> list:
        """원자재 선물 다중 기간 수익률 수집"""
        results = []
        today = datetime.datetime.now(KST)
        ytd_start = datetime.datetime(today.year, 1, 1, tzinfo=datetime.timezone.utc)

        def _pct(a, b):
            if b and b != 0:
                v = (a - b) / b * 100
                return f"{'+' if v >= 0 else ''}{v:.2f}%"
            return "-"

        for name, (ticker, flag) in COMMODITY_TICKERS.items():
            try:
                hist = yf.Ticker(ticker).history(period="1y")
                hist = hist.dropna(subset=["Close"])
                if len(hist) < 2:
                    raise ValueError("데이터 부족")
                close = hist["Close"].iloc[-1]
                prev  = hist["Close"].iloc[-2]
                w1    = hist["Close"].iloc[-6]  if len(hist) >= 6  else None
                m1    = hist["Close"].iloc[-22] if len(hist) >= 22 else None
                ytd_i = hist.index.searchsorted(ytd_start)
                ytd_p = hist["Close"].iloc[ytd_i] if ytd_i < len(hist) else None
                y1    = hist["Close"].iloc[0]    if len(hist) >= 50 else None
                results.append({
                    "name":    name,
                    "flag":    flag,
                    "d1":      _pct(close, prev),
                    "w1":      _pct(close, w1),
                    "m1":      _pct(close, m1),
                    "ytd":     _pct(close, ytd_p),
                    "y1":      _pct(close, y1),
                })
            except Exception:
                results.append({
                    "name": name, "flag": flag,
                    "d1": "-", "w1": "-", "m1": "-", "ytd": "-", "y1": "-",
                })
        return results

    def fetch_sector_heatmap(self) -> dict:
        """섹터 ETF 기반 상승/하락 상위 3개 섹터 수집"""
        sectors = []

        for ticker, name in SECTOR_ETFS.items():
            try:
                hist = yf.Ticker(ticker).history(period="5d")
                hist = hist.dropna(subset=["Close"])
                if len(hist) < 2:
                    continue
                close = hist["Close"].iloc[-1]
                prev  = hist["Close"].iloc[-2]
                pct   = (close - prev) / prev * 100
                sectors.append((name, pct))
            except Exception:
                pass

        if not sectors:
            return {
                "top_gainers": ["[확인 필요]"],
                "top_losers":  ["[확인 필요]"],
            }

        sectors.sort(key=lambda x: x[1], reverse=True)

        def fmt(name, pct):
            sign = "+" if pct >= 0 else ""
            return f"{name} {sign}{pct:.2f}%"

        return {
            "top_gainers": [fmt(n, p) for n, p in sectors[:3]],
            "top_losers":  [fmt(n, p) for n, p in sectors[-3:]],
        }

    def fetch_ls_etf_top20(self) -> list:
        """LeverageShares 2x ETF 수익률 TOP 20 수집 (Finviz 기반)"""
        from finviz import get_stock
        etf_tickers = _get_ls_etf_tickers()
        print("  (Finviz — LeverageShares ETF 수집 중)")
        results = []

        for ticker, name, leverage in etf_tickers:
            entry = {
                "ticker":    ticker,
                "name":      name,
                "volume":    "-",
                "return_1d": "[확인 필요]",
                "leverage":  leverage,
                "_pct_raw":  -999,
            }
            try:
                d      = get_stock(ticker)
                change = d.get("Change", "")        # e.g. "2.66%"
                price  = d.get("Price",  "-")
                volume = d.get("Volume", "-")

                # 부호 정규화: Finviz는 "-2.17%" / "2.66%" 형태
                try:
                    pct_val = float(change.replace("%", ""))
                    sign    = "+" if pct_val >= 0 else ""
                    ret_str = f"{sign}{pct_val:.2f}%"
                    pct_raw = pct_val
                except ValueError:
                    ret_str = change
                    pct_raw = -999

                entry.update({
                    "price":     price,
                    "volume":    volume,
                    "return_1d": ret_str,
                    "_pct_raw":  pct_raw,
                })
            except Exception:
                pass

            results.append(entry)

        # 수익률 내림차순 정렬 후 순위 부여
        results.sort(key=lambda r: r["_pct_raw"], reverse=True)
        for i, r in enumerate(results, 1):
            r["rank"] = i
            del r["_pct_raw"]

        return results[:20]

    def fetch_yields(self) -> dict:
        """미국 국채 금리 수집 (2Y·10Y·30Y)"""
        result = {}
        labels = {"UST_2Y": "2Y", "UST_10Y": "10Y", "UST_30Y": "30Y"}
        for key, ticker in YIELD_TICKERS.items():
            label = labels[key]
            try:
                hist = yf.Ticker(ticker).history(period="5d")
                hist = hist.dropna(subset=["Close"])
                if len(hist) < 2:
                    raise ValueError("데이터 부족")
                close = hist["Close"].iloc[-1]
                prev  = hist["Close"].iloc[-2]
                chg   = close - prev
                sign  = "+" if chg >= 0 else ""
                result[label] = f"{close:.2f}% ({sign}{chg:.2f}bp)"
            except Exception:
                result[label] = "[확인 필요]"
        return result

    def fetch_macro(self) -> dict:
        """글로벌 매크로 지표 수집 (DXY / Gold / Oil)"""
        result = {}
        units = {"DXY": "", "Gold": " $/oz", "Oil": " $/bbl"}
        for name, ticker in MACRO_TICKERS.items():
            try:
                hist = yf.Ticker(ticker).history(period="5d")
                hist = hist.dropna(subset=["Close"])
                if len(hist) < 2:
                    raise ValueError("데이터 부족")
                close = hist["Close"].iloc[-1]
                prev  = hist["Close"].iloc[-2]
                pct   = (close - prev) / prev * 100
                sign  = "+" if pct >= 0 else ""
                unit  = units.get(name, "")
                result[name] = f"{close:.2f}{unit} ({sign}{pct:.2f}%)"
            except Exception:
                result[name] = "[확인 필요]"
        return result

    def fetch_sparkline(self, days: int = 7) -> list[float]:
        """S&P 500 최근 N일 종가 리스트 (HTML 차트용)"""
        try:
            hist = yf.Ticker("^GSPC").history(period=f"{days + 5}d")
            hist = hist.dropna(subset=["Close"])
            closes = hist["Close"].tail(days).tolist()
            return [round(v, 2) for v in closes]
        except Exception:
            return []

    def fetch_fear_greed(self) -> dict:
        """CNN Fear & Greed Index 수집"""
        import urllib.request
        try:
            req = urllib.request.Request(
                FEAR_GREED_URL,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0",
                    "Accept":     "*/*",
                    "Referer":    "https://edition.cnn.com/markets/fear-and-greed",
                    "Origin":     "https://edition.cnn.com",
                },
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
            fg = data.get("fear_and_greed", {})
            score  = round(fg.get("score", 0))
            rating = fg.get("rating", "").replace("_", " ").title()
            return {"score": score, "rating": rating}
        except Exception:
            return {"score": None, "rating": "[확인 필요]"}

    def collect_all(self) -> dict:
        """모든 시장 데이터 수집 후 통합 dict 반환"""
        now_kst = datetime.datetime.now(KST)

        print("📈 지수 데이터 수집 중...")
        indices, last_date = self.fetch_indices()

        print("🗂️  섹터 히트맵 수집 중...")
        heatmap = self.fetch_sector_heatmap()

        print("📊 LeverageShares ETF 데이터 수집 중...")
        etf_list = self.fetch_ls_etf_top20()

        print("💵 국채 금리 수집 중...")
        yields = self.fetch_yields()

        print("🌍 글로벌 매크로 수집 중...")
        macro = self.fetch_macro()

        print("😱 Fear & Greed Index 수집 중...")
        fear_greed = self.fetch_fear_greed()

        print("📉 S&P 500 스파크라인 수집 중...")
        sparkline = self.fetch_sparkline()

        print("🪙 원자재 데이터 수집 중...")
        commodities = self.fetch_commodities()

        print("📡 Finviz 기초자산 주가 수집 중...")
        underlying = FinvizFetcher().fetch_underlyings(etf_list)
        etf_fetch_time = datetime.datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")

        report_date = now_kst.strftime("%Y-%m-%d")   # 리포트 생성일 (파일명용)
        close_date  = last_date                      # yfinance 실제 마지막 거래일

        return {
            "date":               report_date,
            "close_date":         close_date,
            "update_time":        now_kst.strftime("%H:%M KST"),
            "etf_fetch_time":     etf_fetch_time,
            "indices":            indices,
            "sector_heatmap":     heatmap,
            "yields":             yields,
            "macro":              macro,
            "fear_greed":         fear_greed,
            "sparkline_sp500":    sparkline,
            "commodities":        commodities,
            "underlying_stocks":  underlying,
            "key_issues":         [],        # NewsFetcher에서 채움
            "leverage_etf_top20": etf_list,
        }


# ============================================================
# 3. NEWS FETCHER
# ============================================================

# MarketWatch "Moneyist" 같은 개인 재무상담/오피니언 칼럼은 1인칭 서술로 시작하는
# 경우가 거의 예외 없음 (실제 뉴스 헤드라인은 3인칭/사건 주도형). 예:
#   "My son does not work..." / "We're in our 50s and have $1.5 million..."
# vs "Fed's Powell says..." / "Dollar jumps 0.5%..."
_PERSONAL_ADVICE_PATTERN = re.compile(r"^(i|my|our|we)\b", re.IGNORECASE)


def _is_personal_advice_column(title: str) -> bool:
    """1인칭 서술로 시작하는 개인 재무상담/오피니언 칼럼 제목인지 판별."""
    return bool(_PERSONAL_ADVICE_PATTERN.match(title.strip()))


class NewsFetcher:
    """RSS 피드 기반 주요 금융 뉴스 헤드라인 수집"""

    MAX_ITEMS = 5

    def _fetch_from(self, feeds: list, max_items: int) -> list[dict]:
        """
        공통 RSS 수집 로직.
        개인 재무상담/오피니언 칼럼(_is_personal_advice_column)은 걸러내고,
        필터링 후에도 max_items를 채울 수 있도록 여유분(버퍼)까지 모아온다.
        """
        fetch_limit = max_items * 3
        items, seen = [], set()
        for url in feeds:
            if len(items) >= fetch_limit:
                break
            try:
                feed = feedparser.parse(url)
                for entry in feed.entries:
                    title = entry.get("title", "").strip()
                    link  = entry.get("link",  "")
                    if (
                        title
                        and title not in seen
                        and not _is_personal_advice_column(title)
                    ):
                        seen.add(title)
                        items.append({"title": title, "url": link})
                    if len(items) >= fetch_limit:
                        break
            except Exception as e:
                print(f"  ⚠️  RSS 수집 실패 ({url}): {e}")
        return items[:max_items]

    def fetch(self) -> list[dict]:
        """주요 금융 뉴스"""
        items = self._fetch_from(NEWS_RSS_FEEDS, self.MAX_ITEMS)
        return items or [{"title": "[뉴스 데이터 자동 수집 실패]", "url": ""}]

    def fetch_geo(self) -> list[dict]:
        """지정학적 이슈 뉴스"""
        items = self._fetch_from(GEO_RSS_FEEDS, 6)
        return items or [{"title": "[지정학 뉴스 수집 실패]", "url": ""}]

    def fetch_corp(self) -> list[dict]:
        """기업별 주요 이슈 뉴스 (Bloomberg RSS)"""
        items, seen = [], set()
        for url in CORP_RSS_FEEDS:
            if len(items) >= 20:
                break
            try:
                feed = feedparser.parse(url)
                for entry in feed.entries:
                    title = entry.get("title", "").strip()
                    link  = entry.get("link", "")
                    if not title or title in seen:
                        continue
                    seen.add(title)
                    # 날짜 파싱
                    published = ""
                    if hasattr(entry, "published_parsed") and entry.published_parsed:
                        import time
                        t = entry.published_parsed
                        published = f"{t.tm_year}-{t.tm_mon:02d}-{t.tm_mday:02d}"
                    items.append({
                        "ticker": "",
                        "title":  title,
                        "url":    link,
                        "date":   published,
                        "source": "Bloomberg",
                    })
                    if len(items) >= 20:
                        break
            except Exception as e:
                print(f"  ⚠️  Bloomberg RSS 수집 실패 ({url}): {e}")
        return items or [{"ticker": "", "title": "[기업 뉴스 수집 실패]", "url": "", "date": "", "source": ""}]


class CalendarFetcher:
    """NASDAQ 공개 API로 경제 캘린더 / 실적발표 수집 (API 키 불필요)"""

    HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json, text/plain, */*",
    }

    def fetch_economic(self, days: int = 30) -> list:
        """경제 캘린더 (Investing.com 공개 데이터)"""
        import urllib.request
        today = datetime.datetime.now(KST)
        end   = today + datetime.timedelta(days=days)
        url = (
            "https://api.nasdaq.com/api/calendar/economicevents"
            f"?date={today.strftime('%Y-%m-%d')}"
            f"&dateRange=custom"
            f"&startdate={today.strftime('%Y-%m-%d')}"
            f"&enddate={end.strftime('%Y-%m-%d')}"
            "&type=all"
        )
        try:
            req = urllib.request.Request(url, headers=self.HEADERS)
            with urllib.request.urlopen(req, timeout=10) as r:
                data = json.loads(r.read().decode())
            rows = (data.get("data") or {}).get("rows") or []
            result = []
            for row in rows[:20]:
                result.append({
                    "date":     row.get("eventDate",    "-"),
                    "time":     row.get("eventTime",    "-"),
                    "event":    row.get("eventName",    "-"),
                    "forecast": row.get("consensus",    "-"),
                    "previous": row.get("previous",     "-"),
                })
            return result
        except Exception as e:
            print(f"  ⚠️  경제 캘린더 수집 실패: {e}")
            return []

    def fetch_earnings(self, days: int = 30) -> list:
        """실적발표 일정 (earnings.kr API 기반)"""
        import urllib.request
        today  = datetime.datetime.now(KST)
        result = []
        seen   = set()

        for offset in range(0, days):
            if len(result) >= 30:
                break
            date_str = (today + datetime.timedelta(days=offset)).strftime("%Y-%m-%d")
            url = f"https://earnings.kr/api/getCalendarByDate?date={date_str}"
            try:
                req = urllib.request.Request(url, headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0",
                    "Referer":    "https://earnings.kr/calendar",
                })
                with urllib.request.urlopen(req, timeout=8) as r:
                    data = json.loads(r.read().decode())

                # pre: 개장 전, after: 장 마감 후, notSupplied: 미정
                for timing, label in [("pre", "개장 전"), ("after", "장 마감 후"), ("notSupplied", "미정")]:
                    for row in data.get(timing, []):
                        ticker = row.get("symbol", "")
                        if not ticker or ticker in seen:
                            continue
                        seen.add(ticker)
                        eps_est = row.get("epsEstimate")
                        rev_est = row.get("revenueEstimate")
                        result.append({
                            "date":    date_str,
                            "company": row.get("name", "-"),
                            "ticker":  ticker,
                            "eps_est": f"${eps_est:.2f}" if eps_est is not None else "-",
                            "rev_est": f"${rev_est/1e9:.2f}B" if rev_est is not None else "-",
                            "time":    label,
                        })
            except Exception:
                pass

        return result[:30]


# ============================================================
# 3-B. FINVIZ FETCHER (기초자산 주가 보완)
# ============================================================

def _is_recent_finviz_date(date_str: str, max_age_days: int, now: "datetime.datetime | None" = None) -> bool:
    """
    Finviz get_news()가 반환하는 'YYYY-MM-DD HH:MM' 형식 날짜가
    now 기준 max_age_days 이내인지 판별. 파싱 실패 시 안전하게 False.
    """
    if now is None:
        now = datetime.datetime.now()
    try:
        published = datetime.datetime.strptime(date_str, "%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return False
    cutoff = now - datetime.timedelta(days=max_age_days)
    return published >= cutoff


class FinvizFetcher:
    """finviz 패키지로 ETF 기초자산 주가 수집 (API 키 불필요)"""

    def fetch_underlyings(self, etf_list: list) -> dict:
        """
        etf_list의 티커에서 기초자산 주가를 finviz로 조회.
        반환: {기초자산_ticker: {"price": ..., "change": ...}}
        """
        try:
            from finviz import get_stock
        except ImportError:
            print("  ⚠️  finviz 패키지 없음: pip install finviz")
            return {}

        needed = list({
            ETF_UNDERLYING[e["ticker"]]
            for e in etf_list
            if e["ticker"] in ETF_UNDERLYING
        })

        results = {}
        for ticker in needed:
            try:
                data     = get_stock(ticker)
                price    = data.get("Price",  "-")
                change   = data.get("Change", "-")
                volume   = data.get("Volume", "-")
                results[ticker] = {
                    "price":  price,
                    "change": change,  # e.g. "-3.45%"
                    "volume": volume,
                }
            except Exception as e:
                results[ticker] = {"price": "-", "change": "-", "volume": "-"}

        return results

    def fetch_corp_news(self, tickers: list, max_per_ticker: int = 2, max_age_days: int | None = None) -> list:
        """
        Finviz에서 종목별 최신 뉴스 수집.
        반환: [{"title": ..., "url": ..., "ticker": ..., "date": ...}]
        날짜 최신순 정렬. max_age_days보다 오래된 기사는 제외 (기본 config.json의
        corp_news_max_age_days, 없으면 3일). 여러 종목 피드에 겹쳐 실리는
        기사(예: 여러 기업을 다루는 기사)는 제목 기준으로 중복 제거.
        기사 제목에서 실제 언급된 회사를 감지해 ticker를 자동 보정.
        """
        if max_age_days is None:
            max_age_days = CFG.get("corp_news_max_age_days", 3)

        try:
            from finviz import get_news
        except ImportError:
            print("  ⚠️  finviz 패키지 없음: pip install finviz")
            return []

        # 회사명 키워드 → ticker 매핑 (대소문자 무관 매칭)
        COMPANY_KEYWORDS: dict[str, str] = {
            # 추적 종목
            "nvidia": "NVDA", "nvda": "NVDA",
            "tesla": "TSLA", "tsla": "TSLA",
            "amd": "AMD", "advanced micro": "AMD",
            "palantir": "PLTR", "pltr": "PLTR",
            "coinbase": "COIN",
            "broadcom": "AVGO", "avgo": "AVGO",
            "arm holdings": "ARM", "arm ": "ARM",
            "robinhood": "HOOD", "hood": "HOOD",
            "snap": "SNAP", "snapchat": "SNAP",
            "tsmc": "TSM", "taiwan semiconductor": "TSM",
            "cloudflare": "NET",
            "spotify": "SPOT",
            "paypal": "PYPL", "pypl": "PYPL",
            "palo alto": "PANW", "panw": "PANW",
            "salesforce": "CRM", "crm": "CRM",
            "nio": "NIO",
            "duolingo": "DUOL", "duol": "DUOL",
            "xpeng": "XPEV", "xpev": "XPEV",
            "lululemon": "LULU", "lulu": "LULU",
            "unitedhealth": "UNH", "unh": "UNH",
            # 자주 등장하는 타사
            "apple": "AAPL", "aapl": "AAPL",
            "microsoft": "MSFT", "msft": "MSFT",
            "google": "GOOG", "alphabet": "GOOG", "googl": "GOOG",
            "amazon": "AMZN", "amzn": "AMZN",
            "meta": "META", "facebook": "META",
            "intel": "INTC", "intc": "INTC",
            "samsung": "SSNLF",
            "nike": "NKE", "nke": "NKE",
            "netflix": "NFLX", "nflx": "NFLX",
            "uber": "UBER",
            "crowdstrike": "CRWD", "crwd": "CRWD",
            "openai": "MSFT",           # OpenAI → MSFT(최대주주)
            "anthropic": "GOOG",        # Anthropic TPU → Google
            "telegram": "META",         # Telegram 비상장 → META(메신저 섹터)
            "byd": "BYDDF",
            "raytheon": "RTX", "rtx": "RTX",
            "lockheed": "LMT", "lmt": "LMT",
            "bitmine": "BMNR", "bmnr": "BMNR",
            "viking": "VIK",
        }

        def infer_ticker(title: str, original: str) -> str:
            """제목에서 실제 언급 회사를 감지해 ticker 반환. 못 찾으면 original 유지."""
            title_lower = title.lower()
            for keyword, mapped in COMPANY_KEYWORDS.items():
                if keyword in title_lower:
                    return mapped
            return original

        results     = []
        seen_titles = set()
        for ticker in tickers:
            try:
                news  = get_news(ticker)
                fresh = [item for item in news if _is_recent_finviz_date(item[0], max_age_days)]
                for item in fresh[:max_per_ticker]:
                    date, title, url, source = item[0], item[1], item[2], item[3]
                    if title in seen_titles:
                        # 여러 종목 피드에 겹쳐 실린 동일 기사 (예: 여러 기업을 다루는 기사)
                        continue
                    seen_titles.add(title)
                    # 상대경로 → 절대 URL 변환
                    if url.startswith("/"):
                        url = f"https://finviz.com{url}"
                    # 기사 제목 기반 ticker 자동 보정
                    actual_ticker = infer_ticker(title, ticker)
                    results.append({
                        "ticker": actual_ticker,
                        "title":  f"[{actual_ticker}] {title}",
                        "url":    url,
                        "date":   date,
                        "source": source,
                    })
            except Exception:
                pass

        # 날짜 최신순 정렬
        results.sort(key=lambda x: x["date"], reverse=True)
        return results


# ============================================================
# 4. PROMPT BUILDER  (pseudo_prompt.py 기반)
# ============================================================

def _render_etf_table(etf_list: list) -> str:
    """ETF 리스트를 마크다운 테이블 행으로 변환"""
    rows = []
    for etf in etf_list:
        row = (
            f"| {etf['rank']} "
            f"| {etf['ticker']} "
            f"| {etf['name']} "
            f"| ${etf.get('price', '-')} "
            f"| {etf['return_1d']} "
            f"| {etf['volume']} |"
        )
        rows.append(row)
    return "\n".join(rows)


def translate_titles_ko(items: list[dict]) -> list[dict]:
    """기업 뉴스 제목을 Gemini로 한국어 번역. 원본 items를 in-place 수정."""
    if not items:
        return items
    titles = [item.get("title", "") for item in items]
    numbered = "\n".join(f"{i+1}. {t}" for i, t in enumerate(titles))
    prompt = (
        "아래 영문 주식/금융 뉴스 제목들을 자연스러운 한국어로 번역해주세요.\n"
        "반드시 번호 순서대로, 번호와 번역 제목만 출력하세요. 예: 1. 번역된 제목\n\n"
        f"{numbered}"
    )
    try:
        result = call_gemini(prompt=prompt, system="You are a financial news translator.")
        for line in result.strip().splitlines():
            line = line.strip()
            if not line:
                continue
            if ". " in line:
                idx_str, _, translated = line.partition(". ")
                try:
                    idx = int(idx_str.strip()) - 1
                    if 0 <= idx < len(items):
                        items[idx]["title"] = translated.strip()
                except ValueError:
                    pass
    except Exception as e:
        print(f"  ⚠️  번역 실패: {e}")
    return items


def _issue_title(item) -> str:
    """key_issues/geo_issues/corp_issues 항목(dict 또는 str)에서 제목 문자열 추출."""
    return item["title"] if isinstance(item, dict) else item


# ============================================================
# 6.5. HOT TOPICS — 숏츠/카드뉴스 소재 TOP 10 (개부자 채널용)
# ============================================================

# 검색 다양성 확보용 카테고리 — 대형 매크로 이슈가 검색 결과를 독점하는 것을 방지
HOT_TOPIC_CATEGORIES = [
    {
        "name": "매크로/정책",
        "hint": "재무장관, 연준(Fed) 인사 등 주요 정책 인물의 발언이나 통화/재정 정책 이슈",
    },
    {
        "name": "크립토/규제",
        "hint": "암호화폐 관련 법안, 규제 동향 (SEC/CFTC 등 감독기관 움직임 포함)",
    },
    {
        "name": "개별 종목 이상 급등락",
        "hint": "시가총액 크기와 무관하게 당일 5% 이상 급등하거나 급락한 개별 종목",
    },
    {
        "name": "AI/빅테크 투자·IPO 기대감",
        "hint": "비상장 기업의 상장(IPO) 기대감, 대규모 투자·M&A 발표",
    },
]

# 출처 링크 우선순위 — 영어권 1차 소스 (실측 결과, grounding chunk의 title 필드에
# 기사 제목이 아니라 도메인 문자열이 담겨 오므로 이 값들과 substring 매칭에 사용)
PREFERRED_SOURCE_DOMAINS = ["bloomberg", "reuters", "marketwatch", "wsj", "cnbc"]


def build_hot_topics_prompt(market_data: dict, watchlist: list[str] | None = None) -> str:
    """
    RSS 후보(key_issues/geo_issues/corp_issues)에 의존하지 않고,
    Gemini의 google_search grounding으로 카테고리별 실시간 검색을 지시하는 프롬프트 생성.

    watchlist: None이면 config.json의 hot_topics_watchlist를 기본값으로 사용.
               매번 별도로 검색해야 하는 고정 관심 키워드/티커 목록.
    """
    if watchlist is None:
        watchlist = CFG.get("hot_topics_watchlist", [])

    date = market_data.get("date", "오늘")
    categories_text = "\n".join(
        f"{i}. {c['name']} — {c['hint']}"
        for i, c in enumerate(HOT_TOPIC_CATEGORIES, 1)
    )
    preferred_domains_text = ", ".join(PREFERRED_SOURCE_DOMAINS)

    watchlist_section = ""
    if watchlist:
        watchlist_text = "\n".join(f"- {item}" for item in watchlist)
        watchlist_section = f"""
[고정 관심 리스트]
아래 항목들은 매번 별도로 각각 검색해서, 오늘 실제로 관련된 뉴스가 있으면
반드시 후보에 포함시키세요. 검색해봐도 오늘 관련 뉴스가 실제로 없으면
억지로 포함하지 말고 건너뛰세요.

{watchlist_text}
"""

    return f"""
[역할]
당신은 경제/증시 유튜브 채널 '개부자'의 콘텐츠 기획자입니다.

[요청]
Google 검색을 활용해서 {date} 기준 미국 증시와 관련해 실제로 화제가 되고 있는
뉴스·이슈를 아래 4개 카테고리별로 각각 따로 검색해서 찾아주세요.
카테고리마다 최소 2~3개의 후보를 확보하세요. 한 카테고리만 검색하고 끝내지 마세요.
검색 시 가능하면 {preferred_domains_text} 같은 영어권 1차 소스를 우선 참고하세요.

{categories_text}
{watchlist_section}
개인 재무 상담 게시글이나 커뮤니티 잡담처럼 시황과 무관한 내용은 제외하세요.

[최종 선정 규칙]
"오늘의 화제" TOP 10을 아래 순서로 구성하세요:
1. 고정 관심 리스트 중 오늘 실제 관련 뉴스가 있는 항목은 우선 포함
   (카테고리와 겹치면 해당 카테고리 쿼터를 채운 것으로 간주해도 됩니다)
2. 남는 자리는 위 4개 카테고리 각각에서 최소 1~2개는 포함되도록 구성
3. 나머지는 화제성 높은 순으로 채우기

가장 화제성 높은 순으로 정리하더라도 한 카테고리(예: 금리·CPI 같은 매크로 이슈)가
TOP 10을 독점하지 않게 하세요. 전체 개수는 항상 TOP 10을 넘지 않게 하세요.

[숏츠 vs 카드뉴스 분류 기준]
영상 마지막에 Leverage Shares ETF 상품을 자연스럽게 연결하는 홍보 컷이 붙기 때문에,
아래 기준으로 명확히 구분하세요:

- 숏츠: 특정 종목/티커 하나가 "주인공"인 뉴스. 그 종목/티커로 영상 마지막에
  자연스럽게 연결할 수 있어야 합니다.
  예: NVDA 급등, TSLA 실적 발표, MRNA 주가 변동 — 기업/종목이 중심인 사건.

- 카드뉴스: 매크로/섹터/트렌드처럼 범위가 넓은 "시황" 성격의 주제.
  범위가 넓어도 관련 종목들로 자연스럽게 확장 설명이 가능하면 더 적합하지만,
  이 연결은 필수가 아니며 유연하게 판단하세요.
  예: "코인 시장 전체 흐름 → 코인베이스/서클/스트래티지로 확장 설명" 같은 경우.

핵심 질문:
- "이 토픽이 특정 종목/티커 하나가 주인공인가?" → 숏츠
- "매크로/섹터/트렌드처럼 범위가 넓은 주제인가?" → 카드뉴스

각 항목은 아래 형식을 반드시 지켜서, 한 줄에 하나씩 출력하세요:

번호. 제목 | 이유(1줄) | 포맷:숏츠 또는 포맷:카드뉴스

예시:
1. 엔비디아(NVDA) 실적 서프라이즈, 목표가 상향 랠리 | 특정 종목이 주인공인 사건, 티커 연결이 쉬움 | 포맷:숏츠
2. 연준 금리 동결, 시장 안도 랠리 | 매크로 이슈, 범위가 넓은 시황성 주제 | 포맷:카드뉴스

다른 설명이나 머리말 없이, 위 형식의 줄만 출력하세요.
"""


def parse_hot_topics_response(text: str) -> list[dict]:
    """
    Gemini가 반환한 '번호. 제목 | 이유 | 포맷:형식' 형태의 텍스트를
    [{rank, title, reason, format}, ...] 리스트로 파싱.
    형식이 안 맞는 줄은 건너뛰고, 최대 10개까지만 반환.
    """
    topics = []
    for line in text.strip().splitlines():
        line = line.strip()
        if not line:
            continue

        m = re.match(r"^\d+\.\s*(.+)$", line)
        if not m:
            continue

        parts = [p.strip() for p in m.group(1).split("|")]
        if len(parts) < 3:
            continue

        title, reason, fmt_part = parts[0], parts[1], parts[2]
        if not title or not reason:
            continue

        fmt = "카드뉴스" if "카드뉴스" in fmt_part else "숏츠"

        topics.append({
            "rank":   len(topics) + 1,
            "title":  title,
            "reason": reason,
            "format": fmt,
        })
        if len(topics) >= 10:
            break

    return topics


def attach_hot_topic_sources(
    text: str,
    topics: list[dict],
    chunks: list[dict],
    supports: list[dict],
) -> list[dict]:
    """
    Gemini google_search grounding 결과(chunks/supports)를 파싱된 hot topics에 매칭.

    각 토픽의 title이 text(모델 원문) 안에서 몇 번째 글자에 있는지 찾고,
    그 구간과 겹치는 grounding support들을 모아 후보 URL 목록을 만든다.
    rank 순서대로 처리하며:
      - 후보 중 아직 다른 토픽에 쓰이지 않은 URL이 있으면, 그중에서도
        PREFERRED_SOURCE_DOMAINS와 매칭되는 후보를 우선 채택 (없으면 첫 후보)
      - 후보가 원래 하나도 없으면 (근거 없음) 출처 없이 유지
      - 후보는 있었지만 전부 이미 앞선 토픽이 선점한 URL뿐이면
        같은 사건을 다루는 중복 토픽으로 간주해 결과에서 제외
    병합으로 빠진 자리는 rank를 1..N으로 다시 채운다.

    chunks:   [{"uri": str, "title": str}, ...]  — title에는 기사 제목이 아니라
              실측상 도메인 문자열(예: "reuters.com")이 담겨 오는 경우가 많음
    supports: [{"start_index": int, "end_index": int, "chunk_indices": [int, ...]}, ...]
    """
    def _candidate_sources(topic):
        start = text.find(topic["title"])
        if start == -1:
            return []
        end = start + len(topic["title"])

        candidates = []
        for support in supports:
            s_start = support.get("start_index", 0)
            s_end   = support.get("end_index", 0)
            if s_start < end and s_end > start:   # 구간 겹침
                for idx in (support.get("chunk_indices") or []):
                    if 0 <= idx < len(chunks):
                        chunk = chunks[idx]
                        candidates.append((chunk.get("uri"), chunk.get("title")))
        return candidates

    def _is_preferred(source_title) -> bool:
        title_lower = (source_title or "").lower()
        return any(domain in title_lower for domain in PREFERRED_SOURCE_DOMAINS)

    used_urls = set()
    result    = []

    for topic in topics:
        candidates = _candidate_sources(topic)
        available  = [(uri, title) for uri, title in candidates if uri and uri not in used_urls]

        assigned = None
        if available:
            preferred = [c for c in available if _is_preferred(c[1])]
            assigned  = preferred[0] if preferred else available[0]

        if assigned:
            topic["source_url"], topic["source_title"] = assigned
            used_urls.add(assigned[0])
            result.append(topic)
        elif not candidates:
            # 근거가 전혀 없음 — 링크 없이 유지
            topic["source_url"]   = None
            topic["source_title"] = None
            result.append(topic)
        # else: 근거는 있었지만 전부 이미 다른(앞선) 토픽이 쓴 URL → 중복으로 판단, 제외

    for i, topic in enumerate(result, 1):
        topic["rank"] = i

    return result


def _extract_grounding(response) -> tuple[list[dict], list[dict]]:
    """
    google-genai SDK 응답에서 grounding_metadata를 plain dict 리스트로 변환.
    검색 그라운딩이 없었거나 응답 구조가 예상과 다르면 빈 리스트를 반환.
    """
    try:
        gm = response.candidates[0].grounding_metadata
    except (AttributeError, IndexError, TypeError):
        return [], []

    if not gm:
        return [], []

    chunks = [
        {"uri": c.web.uri, "title": c.web.title}
        for c in (gm.grounding_chunks or [])
        if getattr(c, "web", None)
    ]
    supports = [
        {
            "start_index":   s.segment.start_index,
            "end_index":     s.segment.end_index,
            "chunk_indices": list(s.grounding_chunk_indices or []),
        }
        for s in (gm.grounding_supports or [])
        if getattr(s, "segment", None)
    ]
    return chunks, supports


def fetch_hot_topics(market_data: dict) -> list[dict]:
    """
    Gemini + google_search grounding으로 오늘의 화제 TOP 10을 검색·생성하고
    각 토픽에 출처 링크(source_url/source_title)를 매칭해서 반환.

    grounding이 완전히 비어 있으면(모델이 이번 호출에서 검색 인용을 하나도
    기록하지 않은 경우) 1회만 재시도한다 — Gemini google_search grounding은
    호출마다 인용 메타데이터를 줄지 여부가 비결정적이라, 완전히 없앨 수는
    없지만 빈도는 줄일 수 있다.
    """
    from google.genai import types as genai_types

    prompt = build_hot_topics_prompt(market_data)
    system = "You are a content planner for a Korean finance YouTube channel."
    tools  = [genai_types.Tool(google_search=genai_types.GoogleSearch())]

    response          = _generate_content(prompt=prompt, system=system, tools=tools)
    chunks, supports  = _extract_grounding(response)

    if not chunks:
        response         = _generate_content(prompt=prompt, system=system, tools=tools)
        chunks, supports = _extract_grounding(response)

    text   = response.text
    topics = parse_hot_topics_response(text)
    return attach_hot_topic_sources(text, topics, chunks, supports)


def render_hot_topics_section(hot_topics: list[dict]) -> str:
    """오늘의 화제 TOP 10 섹션 HTML 반환. 빈 리스트면 빈 문자열(섹션 생략)."""
    if not hot_topics:
        return ""

    cards_html = ""
    for t in hot_topics:
        fmt        = t.get("format", "숏츠")
        color      = "#e65100" if fmt == "카드뉴스" else "#1a56db"
        source_url = t.get("source_url")
        src_btn = (
            f'<a class="hot-topic-src-btn" href="{source_url}" target="_blank" rel="noopener">🔗 출처</a>'
            if source_url else ""
        )
        cards_html += f"""
        <div class="hot-topic-card">
          <span class="hot-topic-rank">#{t.get('rank', '-')}</span>
          <span class="hot-topic-format" style="background:{color}">{fmt}</span>
          <div class="hot-topic-title">{t.get('title', '')}{src_btn}</div>
          <div class="hot-topic-reason">{t.get('reason', '')}</div>
        </div>"""

    return f"""
    <section class="hot-topics-section">
      <h2>🔥 오늘의 화제 TOP 10 — 숏츠/카드뉴스 소재</h2>
      <p class="hot-topics-sub">개부자 채널 콘텐츠 기획용 · Powered by Gemini</p>
      <div class="hot-topics-grid">{cards_html}
      </div>
    </section>
    """


def load_latest_hot_topics(out_dir: Path, date: str) -> list[dict]:
    """
    output/hot_topics_{date}.json을 우선 찾고, 없으면(예: 화제 검색이
    AM 실행에서만 도는데 오늘은 PM만 실행된 경우) output/ 폴더에서
    가장 최근 hot_topics_*.json으로 대체 — 단 date 기준 1일 이내로
    생성된 파일만 인정. 그보다 오래된(며칠~몇 년 전) 파일은 무시하고
    빈 리스트를 반환.
    """
    exact = out_dir / f"hot_topics_{date}.json"
    if exact.exists():
        try:
            return json.loads(exact.read_text(encoding="utf-8"))
        except Exception:
            pass

    try:
        target = datetime.datetime.strptime(date, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return []

    candidates = sorted(out_dir.glob("hot_topics_*.json"), reverse=True)
    if not candidates:
        return []

    m = re.match(r"hot_topics_(\d{4}-\d{2}-\d{2})\.json$", candidates[0].name)
    if not m:
        return []
    try:
        file_date = datetime.datetime.strptime(m.group(1), "%Y-%m-%d").date()
    except ValueError:
        return []
    if not (0 <= (target - file_date).days <= 1):
        return []

    try:
        return json.loads(candidates[0].read_text(encoding="utf-8"))
    except Exception:
        return []


def build_prompt(data: dict) -> str:
    issues_text = "\n".join(f"• {_issue_title(i)}" for i in data["key_issues"])
    geo_text    = "\n".join(f"• {_issue_title(i)}" for i in data.get("geo_issues",  []))
    corp_text   = "\n".join(f"• {_issue_title(i)}" for i in data.get("corp_issues", []))
    etf_table   = _render_etf_table(data["leverage_etf_top20"])
    yields      = data.get("yields", {})
    macro       = data.get("macro", {})
    fg          = data.get("fear_greed", {})
    fg_text     = f"{fg.get('score', '?')} / 100 ({fg.get('rating', '?')})"

    prompt = f"""
[역할]
당신은 20년 경력의 월스트리트 애널리스트입니다.
아래 제공된 {data['date']} 미국 증시 데이터를 바탕으로,
출근길 투자자가 5분 안에 핵심을 파악할 수 있는 시황 브리핑을 작성하세요.

[출력 구조 — 반드시 아래 순서와 형식을 지키세요]

## 1. 주요 지수 요약
- S&P 500 : {data['indices']['SP500']['close']} ({data['indices']['SP500']['change_pct']})
- NASDAQ   : {data['indices']['NASDAQ']['close']} ({data['indices']['NASDAQ']['change_pct']})
- DOW      : {data['indices']['DOW']['close']} ({data['indices']['DOW']['change_pct']})
- VIX      : {data['indices']['VIX']['close']} ({data['indices']['VIX']['change_pct']})

## 2. 채권 시장 & 심리 지표
- 미국채  5Y : {yields.get('2Y',  '[확인 필요]')}
- 미국채 10Y : {yields.get('10Y', '[확인 필요]')}
- 미국채 30Y : {yields.get('30Y', '[확인 필요]')}
- Fear & Greed Index : {fg_text}
- 달러 인덱스 (DXY) : {macro.get('DXY',  '[확인 필요]')}
- 금 (Gold)          : {macro.get('Gold', '[확인 필요]')}
- 원유 WTI (Oil)     : {macro.get('Oil',  '[확인 필요]')}

## 3. 섹터 동향
- 상승 상위 섹터: {', '.join(data['sector_heatmap']['top_gainers'])}
- 하락 상위 섹터: {', '.join(data['sector_heatmap']['top_losers'])}

## 4. 오늘의 주요 이슈 (3~5개 불릿)
{issues_text}

## 5. 애널리스트 총평
→ [1~2문장. 시장 전체 흐름과 투자자 시사점을 핵심만 담아 작성]
   금리 동향·심리 지표를 근거로 활용할 것.
   불확실한 내용은 반드시 [확인 필요] 태그를 붙일 것.

## 6. Leverageshares x2 레버리지 ETF — 수익률 TOP 20
| # | Ticker | ETF명 | 거래량 | 수익률 | 배율 |
|---|--------|-------|--------|--------|------|
{etf_table}

## 7. 경제 캘린더 (오늘부터 1개월 이내 주요 일정)
아래 형식의 표로 작성. FOMC, CPI, PCE, 비농업고용, 실업수당, GDP, ISM 등 포함. 최소 8개.
| 날짜 | 시간(ET) | 이벤트 | 예측 | 이전 |
|------|----------|--------|------|------|

## 8. 실적발표 일정 (오늘부터 1개월 이내)
아래 형식의 표로 작성. S&P 500 주요 기업. 최소 10개. 날짜순.
| 날짜 | 기업 (티커) | EPS 예측 | 매출 예측 | 발표 시간 |
|------|------------|---------|---------|---------|

## 9. 지정학적 이슈
아래 뉴스를 한국어로 번역·요약하고 시장 영향을 1줄씩 추가하세요:
{geo_text}

## 10. 기업별 주요 이슈
아래 뉴스를 한국어로 번역·요약하고 ticker와 주가 영향을 포함하세요:
{corp_text}

[작성 규칙]
- 문체   : 간결·직관적. 문장은 짧게. 군더더기 제거.
- 수치   : 제공된 데이터 그대로 사용. 임의로 추정하지 말 것.
- 불확실 : 확인되지 않은 정보는 [확인 필요] 명시.
- 분량   : 총평 2문장 이내 / 이슈 불릿 5개 이내 / 표는 행 고정.
- 언어   : 한국어 (수치·ticker는 영문 그대로 유지).
- 날짜   : {data['date']} 기준 향후 1개월 이내 실제 일정 기입.
""".strip()

    return prompt


# ============================================================
# 5. GEMINI API
# ============================================================

def _generate_content(
    prompt: str,
    system: str,
    max_retries: int | None = None,
    tools: list | None = None,
):
    """
    Google Gemini API 호출 (재시도 로직 포함). raw response 객체를 반환.
    무료 티어: gemini-2.0-flash-lite 기준 1일 1,500회 / 분당 15회

    tools: 예) [types.Tool(google_search=types.GoogleSearch())] — 검색 그라운딩 활성화.
           검색 그라운딩은 일반 텍스트 생성과 별도의 자체 쿼터/과금이 있을 수 있음.
    """
    import time

    try:
        from google import genai
        from google.genai import types as genai_types
    except ImportError:
        raise ImportError("google-genai 패키지가 없습니다: pip install google-genai")

    if not GOOGLE_API_KEY:
        raise ValueError(
            "GOOGLE_API_KEY가 설정되지 않았습니다.\n"
            "  1. https://aistudio.google.com/apikey 에서 키 발급 (무료)\n"
            "  2. .env 파일에 GOOGLE_API_KEY=발급받은키 추가"
        )

    client  = genai.Client(api_key=GOOGLE_API_KEY)
    retries = max_retries if max_retries is not None else CFG["max_retries"]

    # 주 모델 실패 시 자동 폴백 순서
    FALLBACK_MODELS = [
        GEMINI_MODEL,
        "gemini-2.5-flash-lite",
        "gemini-flash-latest",
        "gemini-flash-lite-latest",
    ]

    for model_name in FALLBACK_MODELS:
        for attempt in range(1, retries + 1):
            try:
                response = client.models.generate_content(
                    model=model_name,
                    contents=prompt,
                    config=genai_types.GenerateContentConfig(
                        system_instruction=system,
                        max_output_tokens=CFG["max_tokens"],
                        temperature=0.7,
                        tools=tools,
                    ),
                )
                if model_name != GEMINI_MODEL:
                    print(f"  ✅ 폴백 모델 사용: {model_name}")
                return response

            except Exception as e:
                err = str(e).lower()
                if "api_key" in err or "permission" in err or "invalid" in err:
                    raise
                is_overload = "503" in str(e) or "unavailable" in err or "overload" in err
                wait = 2 ** attempt
                print(f"  ⚠️  {model_name} 오류 (시도 {attempt}/{retries}): {e} — {wait}초 후 재시도")
                time.sleep(wait)
                if is_overload and attempt == retries:
                    print(f"  🔄 {model_name} 과부하 — 다음 모델로 전환")
                    break  # 다음 폴백 모델 시도

    raise RuntimeError("모든 Gemini 모델 호출 실패")


def call_gemini(prompt: str, system: str, max_retries: int | None = None) -> str:
    """Google Gemini API 호출, 텍스트만 필요한 기존 호출부용 wrapper."""
    return _generate_content(prompt, system, max_retries).text


def validate_output(response: str) -> dict:
    """생성된 브리핑이 필수 섹션을 모두 포함하는지 검증"""
    missing = [s for s in REQUIRED_SECTIONS if s not in response]
    return {
        "is_valid":         len(missing) == 0,
        "missing_sections": missing,
    }


# ============================================================
# 6. HTML REPORT GENERATOR
# ============================================================

def _index_card_color(change_pct: str) -> str:
    """등락률 문자열로 카드 배경색 결정"""
    try:
        val = float(change_pct.replace("%", "").replace("+", ""))
        if val > 0:
            return "#1a3a2a"   # 녹색 계열
        elif val < 0:
            return "#3a1a1a"   # 적색 계열
        return "#1e1e2e"
    except ValueError:
        return "#1e1e2e"


def _return_color(ret: str) -> str:
    """수익률 텍스트 색상"""
    try:
        val = float(ret.replace("%", "").replace("+", ""))
        return "#4ade80" if val >= 0 else "#f87171"
    except ValueError:
        return "#94a3b8"


def build_html_report(market_data: dict, brief_md: str, hot_topics: list | None = None, x_topics: list | None = None) -> str:
    """
    시황 데이터 + Claude 브리핑 텍스트를 받아 HTML 리포트 반환.
    brief_md는 마크다운 텍스트 → 섹션별로 파싱해 렌더링.
    hot_topics가 있으면 "오늘의 화제 TOP 10" 섹션을 최상단(헤더 바로 다음,
    주요 지수 섹션 앞)에 추가로 렌더링.
    """
    date           = market_data["date"]
    close_date     = market_data.get("close_date", date)
    update_time    = market_data["update_time"]
    etf_fetch_time = market_data.get("etf_fetch_time", update_time)

    # 오전(AM) / 오후(PM) 구분 — update_time의 시각 기준
    try:
        _hour = int(update_time.split(":")[0])
    except Exception:
        _hour = datetime.datetime.now(KST).hour
    time_label = "오전 시황" if _hour < 12 else "오후 시황"
    indices     = market_data["indices"]
    heatmap     = market_data["sector_heatmap"]
    etf_list    = market_data["leverage_etf_top20"]
    issues      = market_data["key_issues"]
    yields      = market_data.get("yields", {})
    macro       = market_data.get("macro", {})
    fg          = market_data.get("fear_greed", {})
    sparkline   = market_data.get("sparkline_sp500", [])
    commodities        = market_data.get("commodities",       [])
    econ_calendar      = market_data.get("econ_calendar",     [])
    earn_calendar      = market_data.get("earn_calendar",     [])
    underlying_stocks  = market_data.get("underlying_stocks", {})

    def _cls(v):
        s = str(v)
        return "up" if "+" in s else ("down" if "-" in s else "neu")

    # ── 지수 테이블 행 HTML (다중 기간) ─────────────────────────
    index_cards_html = ""
    labels = {"SP500": "S&P 500", "NASDAQ": "NASDAQ", "DOW": "DOW", "VIX": "VIX"}
    for key, label in labels.items():
        info = indices.get(key, {})
        close   = info.get("close",      "-")
        d1      = info.get("change_pct", "-")
        w1      = info.get("change_1w",  "-")
        m1      = info.get("change_1m",  "-")
        ytd     = info.get("change_ytd", "-")
        y1      = info.get("change_1y",  "-")
        index_cards_html += f"""
        <tr>
          <td>{label}</td>
          <td>{close}</td>
          <td class="{_cls(d1)}">{d1}</td>
          <td class="{_cls(w1)}">{w1}</td>
          <td class="{_cls(m1)}">{m1}</td>
          <td class="{_cls(ytd)}">{ytd}</td>
          <td class="{_cls(y1)}">{y1}</td>
        </tr>"""

    # ── 원자재 테이블 행 HTML ────────────────────────────────────
    commodity_rows_html = ""
    for c in commodities:
        commodity_rows_html += f"""
        <tr>
          <td>{c['flag']} {c['name']}</td>
          <td class="{_cls(c['d1'])}">{c['d1']}</td>
          <td class="{_cls(c['w1'])}">{c['w1']}</td>
          <td class="{_cls(c['m1'])}">{c['m1']}</td>
          <td class="{_cls(c['ytd'])}">{c['ytd']}</td>
          <td class="{_cls(c['y1'])}">{c['y1']}</td>
        </tr>"""

    # ── 스파크라인 데이터 ─────────────────────────────────────
    spark_labels = json.dumps([f"D-{len(sparkline)-i}" for i in range(len(sparkline))])
    spark_values = json.dumps(sparkline)
    spark_color  = "#4ade80" if (len(sparkline) >= 2 and sparkline[-1] >= sparkline[0]) else "#f87171"

    # ── 매크로 카드 HTML ──────────────────────────────────────
    macro_cards_html = ""
    macro_icons = {"DXY": "💵", "Gold": "🥇", "Oil": "🛢️"}
    for name, val in macro.items():
        icon = macro_icons.get(name, "📊")
        val_str = str(val)
        if "+" in val_str:
            m_color = "#d32f2f"
        elif "-" in val_str:
            m_color = "#1565c0"
        else:
            m_color = "#333"
        macro_cards_html += f"""
        <div class="macro-mini">
          <div class="m-name">{icon} {name}</div>
          <div class="m-val" style="color:{m_color}">{val}</div>
        </div>"""

    # ── 금리·F&G HTML ────────────────────────────────────────
    fg_score  = fg.get("score")
    fg_rating = fg.get("rating", "?")
    fg_color  = (
        "#4ade80" if fg_score and fg_score >= 60 else
        "#f87171" if fg_score and fg_score <= 40 else
        "#facc15"
    )
    fg_bar_width = fg_score if fg_score else 50
    yield_rows = "".join(
        f'<div class="yield-item"><span class="y-label">🇺🇸 {label}</span>'
        f'<span class="y-val">{val}</span></div>'
        for label, val in yields.items()
    )

    # ── 섹터 HTML ────────────────────────────────────────────
    gainers_html = "".join(f'<li class="gainer">{s}</li>' for s in heatmap.get("top_gainers", []))
    losers_html  = "".join(f'<li class="loser">{s}</li>'  for s in heatmap.get("top_losers",  []))

    # ── 이슈 li 헬퍼: 텍스트 왼쪽 + 🔗 출처 버튼 오른쪽 ────────
    def _issue_li(text: str, url: str = "") -> str:
        link_btn = (
            f'<a class="src-btn" href="{url}" target="_blank" rel="noopener">🔗 출처</a>'
            if url else ""
        )
        return f'<li><span class="issue-text">{text}</span>{link_btn}</li>'

    ko_issues = []
    in_sec4 = False
    for line in brief_md.splitlines():
        if "4." in line and line.lstrip("#").strip().startswith("4."):
            in_sec4 = True
            continue
        if in_sec4:
            if line.lstrip("#").strip() and line.startswith("#"):
                break
            stripped = line.strip().lstrip("-•* ").strip()
            if stripped and not stripped.startswith("#"):
                ko_issues.append(stripped)

    if ko_issues:
        issues_html = ""
        for i, ko in enumerate(ko_issues):
            url = ""
            if i < len(issues) and isinstance(issues[i], dict):
                url = issues[i].get("url", "")
            issues_html += _issue_li(ko, url)
    else:
        issues_html = "".join(
            _issue_li(
                item.get("title", "") if isinstance(item, dict) else str(item),
                item.get("url", "")   if isinstance(item, dict) else "",
            )
            for item in issues
        )

    # ── 섹션 파싱 헬퍼 ─────────────────────────────────────────
    def _parse_section(md: str, sec_num: int) -> list[str]:
        """## N. 또는 ### N. 섹션에서 줄 목록 추출 (Gemini 헤더 형식 무관)"""
        lines_out, active = [], False
        for ln in md.splitlines():
            stripped_hdr = ln.lstrip("#").strip()
            is_header = ln.startswith("#") and stripped_hdr
            if is_header and stripped_hdr.startswith(f"{sec_num}."):
                active = True; continue
            if active:
                if is_header: break
                s = ln.strip()
                if s: lines_out.append(s)
        return lines_out

    # ── 애널리스트 총평 파싱 (## 5.) ─────────────────────────
    analyst_comment = " ".join(
        ln.lstrip("→•*- ").strip()
        for ln in _parse_section(brief_md, 5)
        if ln.strip() and not ln.strip().startswith("|") and not ln.strip().startswith("#")
    ).strip() or "[확인 필요]"

    # ── 경제 캘린더 HTML (실제 데이터 우선, 없으면 Gemini 파싱) ─
    if econ_calendar:
        econ_html = ""
        for r in econ_calendar:
            econ_html += (
                f"<tr><td>{r['date']}</td><td>{r['time']}</td>"
                f"<td>{r['event']}</td><td>{r['forecast']}</td><td>{r['previous']}</td></tr>"
            )
    else:
        econ_rows = [ln for ln in _parse_section(brief_md, 7)
                     if ln.startswith("|") and "---" not in ln and "날짜" not in ln]
        econ_html = ""
        for row in econ_rows:
            cells = [c.strip() for c in row.strip("|").split("|")]
            econ_html += "<tr>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>"

    # ── 실적발표 HTML (실제 데이터 우선, 없으면 Gemini 파싱) ──
    if earn_calendar:
        earn_html = ""
        for r in earn_calendar:
            earn_html += (
                f"<tr><td>{r['date']}</td><td>{r['company']} ({r['ticker']})</td>"
                f"<td>{r['eps_est']}</td><td>{r['rev_est']}</td><td>{r['time']}</td></tr>"
            )
    else:
        earn_rows = [ln for ln in _parse_section(brief_md, 8)
                     if ln.startswith("|") and "---" not in ln and "날짜" not in ln]
        earn_html = ""
        for row in earn_rows:
            cells = [c.strip() for c in row.strip("|").split("|")]
            earn_html += "<tr>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>"

    # ── 지정학적 이슈 파싱 (## 9.) ───────────────────────────
    geo_raw    = market_data.get("geo_issues",  [])
    geo_parsed = [ln.lstrip("-•* ").strip() for ln in _parse_section(brief_md, 9) if ln.strip()]
    if geo_parsed:
        geo_html = ""
        for i, text in enumerate(geo_parsed):
            url = geo_raw[i]["url"] if i < len(geo_raw) and isinstance(geo_raw[i], dict) else ""
            geo_html += _issue_li(text, url)
    else:
        geo_html = "".join(
            _issue_li(
                item.get("title", "") if isinstance(item, dict) else str(item),
                item.get("url", "")   if isinstance(item, dict) else "",
            )
            for item in geo_raw
        ) or _issue_li("[확인 필요]")

    # ── 기업별 이슈: Finviz 실제 기사 링크 렌더링 ──
    corp_raw  = market_data.get("corp_issues", [])
    corp_html = "".join(
        _issue_li(
            f"<span style='color:#1a56db;font-weight:700'>[{item.get('ticker','')}]</span> "
            f"{item.get('title','').split('] ', 1)[-1]}"
            f"<span style='background:#e65100;color:#fff;font-size:10px;font-weight:700;"
            f"padding:1px 5px;border-radius:3px;margin-left:8px'>FinViz</span>"
            f"<span style='color:#999;font-size:11px;margin-left:6px'>{item.get('date','')[:10]}</span>",
            item.get("url", ""),
        )
        for item in corp_raw
        if isinstance(item, dict)
    ) or _issue_li("[확인 필요]")

    # ── ETF 테이블 HTML ──────────────────────────────────────
    etf_rows_html = ""
    for etf in etf_list:
        ret        = etf.get("return_1d", "-")
        etf_price  = etf.get("price", "-")
        ticker     = etf.get("ticker", "-")
        udl_key    = ETF_UNDERLYING.get(ticker, "")
        udl        = underlying_stocks.get(udl_key, {})
        udl_price  = udl.get("price",  "-")
        udl_change = udl.get("change", "-")
        udl_label  = f"{udl_key}<br><small style='font-weight:400;color:#888'>{udl_price}</small>" if udl_key else "-"
        etf_rows_html += f"""
        <tr>
            <td style="text-align:center;color:#888">{etf.get('rank', '-')}</td>
            <td class="ticker" style="text-align:left">{ticker}</td>
            <td style="text-align:left;color:#333">{etf.get('name', '-')}</td>
            <td style="text-align:right;font-weight:600">${etf_price}</td>
            <td class="{_cls(ret)}">{ret}</td>
            <td style="text-align:left">{udl_label}</td>
            <td class="{_cls(udl_change)}">{udl_change}</td>
        </tr>"""

    # ── F&G 게이지 색상 및 범례 ──────────────────────────────
    fg_needle_deg = -90 + (fg_bar_width / 100) * 180  # -90° ~ +90°
    fg_legend_rows = ""
    fg_levels = [
        ("EXTREME FEAR", "0~24",   "#e74c3c", "#fdecea"),
        ("FEAR",         "25~44",  "#e67e22", "#fef3e2"),
        ("NEUTRAL",      "45~55",  "#aaaaaa", "#f5f5f5"),
        ("GREED",        "56~74",  "#27ae60", "#e8f8ee"),
        ("EXTREME GREED","75~100", "#16a085", "#e0f5f1"),
    ]
    row_ids = ["fg-row-extreme-fear", "fg-row-fear", "fg-row-neutral", "fg-row-greed", "fg-row-extreme-greed"]
    for (lvl_name, lvl_range, lvl_color, lvl_bg), row_id in zip(fg_levels, row_ids):
        bold = "font-weight:700;" if fg_rating and fg_rating.upper() in lvl_name else ""
        fg_legend_rows += f"""
        <tr id="{row_id}" style="background:{lvl_bg}">
          <td style="padding:7px 12px;font-weight:700;color:{lvl_color};{bold}">{lvl_name}</td>
          <td style="padding:7px 12px;text-align:center;color:#555">{lvl_range}</td>
        </tr>"""

    html = f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>미국 시황 | {date}</title>
<script>
  // 열어본 날짜로 자동 업데이트
  document.addEventListener('DOMContentLoaded', function() {{
    const today = new Date();
    const y = today.getFullYear();
    const m = String(today.getMonth()+1).padStart(2,'0');
    const d = String(today.getDate()).padStart(2,'0');
    const todayStr = y + '-' + m + '-' + d;
    document.title = '미국 시황 | ' + todayStr;
    const badge = document.getElementById('today-date-badge');
    if (badge) badge.textContent = '🇺🇸 미국 시황 · ' + todayStr;
    const footer = document.getElementById('today-footer-date');
    if (footer) footer.textContent = 'Generated by MarketBrief · ' + todayStr;
  }});
</script>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js"></script>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: "Apple SD Gothic Neo", "Noto Sans KR", -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    background: #f7f8fa;
    color: #222;
    padding: 24px 16px;
    max-width: 860px;
    margin: 0 auto;
    font-size: 14px;
    line-height: 1.6;
  }}

  /* ── 헤더 ── */
  .page-header {{
    background: #fff;
    border-radius: 12px;
    padding: 24px 28px;
    margin-bottom: 20px;
    border: 1px solid #e4e6ea;
  }}
  .page-header .date-badge {{
    display: inline-block;
    background: #1a56db;
    color: #fff;
    font-size: 13px;
    font-weight: 700;
    padding: 4px 12px;
    border-radius: 20px;
    margin-bottom: 10px;
  }}
  .page-header h1 {{
    font-size: 22px;
    font-weight: 800;
    color: #111;
    margin-bottom: 4px;
  }}
  .page-header .subtitle {{
    font-size: 12px;
    color: #888;
  }}

  /* ── 섹션 ── */
  .section {{ margin-bottom: 20px; }}
  .section-header {{
    display: flex;
    align-items: center;
    gap: 10px;
    margin-bottom: 12px;
  }}
  .section-header .bar {{
    width: 6px;
    height: 24px;
    background: #1a56db;
    border-radius: 3px;
    flex-shrink: 0;
  }}
  .section-header h2 {{
    font-size: 17px;
    font-weight: 800;
    color: #111;
  }}

  /* ── 공통 카드 ── */
  .card {{
    background: #fff;
    border: 1px solid #e4e6ea;
    border-radius: 10px;
    padding: 18px 20px;
  }}

  /* ── 테이블 공통 ── */
  .data-table {{
    width: 100%;
    border-collapse: collapse;
    font-size: 13px;
  }}
  .data-table thead tr {{
    background: #f0f4ff;
  }}
  .data-table th {{
    padding: 9px 10px;
    text-align: right;
    font-weight: 700;
    color: #444;
    border-bottom: 2px solid #d0d8f0;
    white-space: nowrap;
  }}
  .data-table th:first-child {{ text-align: left; }}
  .data-table td {{
    padding: 8px 10px;
    text-align: right;
    border-bottom: 1px solid #f0f0f0;
    color: #333;
    white-space: nowrap;
  }}
  .data-table td:first-child {{ text-align: left; font-weight: 600; }}
  .data-table tbody tr:hover td {{ background: #f8f9ff; }}
  .up   {{ color: #d32f2f; font-weight: 700; }}
  .down {{ color: #1565c0; font-weight: 700; }}
  .neu  {{ color: #555; }}

  /* ── 스파크라인 ── */
  .spark-wrap {{
    background: #fff;
    border: 1px solid #e4e6ea;
    border-radius: 10px;
    padding: 16px 20px;
  }}
  .spark-wrap .spark-title {{
    font-size: 13px;
    font-weight: 700;
    color: #555;
    margin-bottom: 10px;
  }}
  .spark-canvas {{ width: 100% !important; height: 80px !important; }}

  /* ── F&G ── */
  .fg-layout {{
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 20px;
    align-items: center;
  }}
  .gauge-wrap {{ text-align: center; }}
  .gauge-svg {{ width: 180px; height: 100px; }}
  .fg-score-big {{
    font-size: 32px;
    font-weight: 900;
    margin-top: 6px;
  }}
  .fg-rating-label {{
    font-size: 13px;
    font-weight: 700;
    color: #555;
    margin-top: 2px;
  }}
  .fg-legend {{ width: 100%; border-collapse: collapse; font-size: 13px; border-radius: 8px; overflow: hidden; }}
  .fg-legend td {{ border: none; }}

  /* ── 섹터 ── */
  .sector-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }}
  .sector-col h3 {{ font-size: 13px; font-weight: 700; color: #555; margin-bottom: 8px; }}
  .sector-col ul {{ list-style: none; }}
  .sector-col li {{
    padding: 6px 0;
    border-bottom: 1px solid #f0f0f0;
    font-size: 13px;
  }}

  /* ── 이슈 ── */
  .issues-list {{ list-style: none; }}
  .issues-list li {{
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 12px;
    padding: 10px 14px;
    border-left: 4px solid #1a56db;
    background: #f0f4ff;
    margin-bottom: 8px;
    border-radius: 0 8px 8px 0;
    font-size: 13px;
    line-height: 1.6;
    color: #222;
  }}
  .issue-text {{ flex: 1; }}
  .src-btn {{
    flex-shrink: 0;
    display: inline-block;
    padding: 2px 8px;
    background: #1a56db;
    color: #fff !important;
    font-size: 11px;
    font-weight: 600;
    border-radius: 4px;
    text-decoration: none !important;
    white-space: nowrap;
    margin-top: 2px;
  }}
  .src-btn:hover {{ background: #1241a8; }}

  /* ── 총평 ── */
  .analyst-box {{
    background: #fffde7;
    border: 1px solid #f9e400;
    border-radius: 10px;
    padding: 18px 20px;
    font-size: 14px;
    line-height: 1.8;
    color: #333;
  }}

  /* ── ETF 테이블 ── */
  .ticker {{ font-weight: 700; color: #1a56db; }}

  /* ── 금리 ── */
  .yield-grid {{
    display: grid;
    grid-template-columns: repeat(2, 1fr);
    gap: 10px;
  }}
  .yield-item {{
    background: #f8f9ff;
    border-radius: 8px;
    padding: 10px 14px;
    display: flex;
    justify-content: space-between;
    align-items: center;
    font-size: 13px;
  }}
  .yield-item .y-label {{ color: #666; }}
  .yield-item .y-val   {{ font-weight: 700; color: #222; }}

  /* ── 매크로 카드 ── */
  .macro-row {{
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 12px;
    margin-bottom: 14px;
  }}
  .macro-mini {{
    background: #f8f9ff;
    border: 1px solid #e4e6ea;
    border-radius: 8px;
    padding: 12px 14px;
    text-align: center;
  }}
  .macro-mini .m-name {{ font-size: 12px; color: #888; margin-bottom: 4px; }}
  .macro-mini .m-val  {{ font-size: 15px; font-weight: 700; }}

  footer {{
    margin-top: 32px;
    text-align: center;
    font-size: 12px;
    color: #aaa;
    padding-bottom: 20px;
  }}

  /* ── 데이터 출처 표 ── */
  .data-src-table {{
    width: 100%;
    border-collapse: collapse;
    font-size: 12px;
    margin-top: 12px;
    color: #555;
  }}
  .data-src-table th {{
    background: #f0f4ff;
    padding: 6px 10px;
    text-align: left;
    font-weight: 700;
    color: #444;
    border-bottom: 2px solid #d0d8f0;
    white-space: nowrap;
  }}
  .data-src-table td {{
    padding: 5px 10px;
    border-bottom: 1px solid #f0f0f0;
    vertical-align: middle;
  }}
  .data-src-table td:first-child {{ color: #333; font-weight: 500; }}
  .data-src-table td:nth-child(2) {{ white-space: nowrap; color: #1a56db; font-weight: 600; }}
  .data-src-table td:nth-child(4) {{ color: #888; font-style: italic; }}
  .data-src-table tbody tr:hover td {{ background: #f8f9ff; }}
  .data-src-table tbody tr:last-child td {{ border-bottom: none; }}

  /* ── 오늘의 화제 TOP 10 (숏츠/카드뉴스 소재) ── */
  .hot-topics-section {{ margin-bottom: 24px; }}
  .hot-topics-section h2 {{ font-size: 17px; font-weight: 800; color: #111; margin-bottom: 4px; }}
  .hot-topics-sub {{ color: #888; font-size: 12px; margin-bottom: 14px; }}
  .hot-topics-grid {{ display: grid; grid-template-columns: 1fr; gap: 10px; }}
  .hot-topic-card {{
    background: #fff; border: 1px solid #e4e6ea; border-radius: 10px;
    padding: 14px 16px; position: relative;
  }}
  .hot-topic-rank {{ color: #888; font-size: 12px; font-weight: 700; margin-right: 6px; }}
  .hot-topic-format {{
    color: #fff; font-size: 11px; padding: 2px 9px; border-radius: 4px; float: right;
  }}
  .hot-topic-title {{ font-weight: 700; color: #111; margin-top: 4px; }}
  .hot-topic-reason {{ color: #666; font-size: 12.5px; margin-top: 4px; }}
  .hot-topic-src-btn {{
    display: inline-block; margin-left: 8px; padding: 1px 8px;
    background: #eef2ff; color: #1a56db !important; font-size: 11px; font-weight: 700;
    border-radius: 4px; text-decoration: none !important; white-space: nowrap;
  }}
  .hot-topic-src-btn:hover {{ background: #dbe4ff; }}

  @media (max-width: 600px) {{
    .fg-layout {{ grid-template-columns: 1fr; }}
    .sector-grid {{ grid-template-columns: 1fr; }}
    .macro-row {{ grid-template-columns: repeat(2, 1fr); }}
    .yield-grid {{ grid-template-columns: 1fr; }}
  }}
</style>
</head>
<body>

<!-- ── 헤더 ── -->
<div class="page-header">
  <div class="date-badge" id="today-date-badge">🇺🇸 미국 시황 · {date}</div>
  <h1>미국 증시 전망 &nbsp;<span style="font-size:14px;font-weight:600;color:#1a56db;background:#e8f0fe;padding:3px 10px;border-radius:12px">{time_label}</span></h1>
  <div class="subtitle">데이터 수집: {update_time} &nbsp;|&nbsp; Powered by Gemini {GEMINI_MODEL}</div>
  <table class="data-src-table">
    <thead><tr><th>섹션</th><th>기준 시각</th><th>출처</th><th>비고</th></tr></thead>
    <tbody>
      <tr><td>📈 주요 지수 (S&amp;P500 · NASDAQ · DOW · VIX)</td><td>{close_date} 종가</td><td>Yahoo Finance</td><td>15분 지연</td></tr>
      <tr><td>💵 글로벌 매크로 (DXY · 금 · 유가)</td><td>{close_date} 종가</td><td>Yahoo Finance</td><td>15분 지연</td></tr>
      <tr><td>🪙 원자재 (금·은·구리·원유 등)</td><td>{close_date} 종가</td><td>Yahoo Finance</td><td>선물 기준</td></tr>
      <tr><td>🗺️ S&amp;P 500 섹터 히트맵</td><td>실시간</td><td>TradingView 위젯</td><td>브라우저에서 직접 렌더링</td></tr>
      <tr><td>📊 섹터 동향 (상승·하락 상위)</td><td>{close_date} 종가</td><td>Yahoo Finance (섹터 ETF)</td><td>XLK·XLF·XLE 등</td></tr>
      <tr><td>😱 공포탐욕 지수</td><td>{update_time} 기준</td><td>CNN Fear &amp; Greed Index</td><td>실시간</td></tr>
      <tr><td>💹 ETF 수익률 TOP 20</td><td>{etf_fetch_time}</td><td>Finviz</td><td>장중 실시간</td></tr>
      <tr><td>🏦 기초자산 주가</td><td>{etf_fetch_time}</td><td>Finviz</td><td>장중 실시간</td></tr>
      <tr><td>🇺🇸 미국 국채 금리</td><td>{close_date} 종가</td><td>Yahoo Finance</td><td>2Y · 10Y · 30Y</td></tr>
      <tr><td>📅 경제 캘린더</td><td>수집 시점</td><td>NASDAQ 공개 API</td><td>향후 30일</td></tr>
      <tr><td>📋 실적발표 일정</td><td>수집 시점</td><td><a href="https://earnings.kr/calendar" target="_blank" style="color:#1a56db">earnings.kr</a></td><td>향후 30일</td></tr>
      <tr><td>📰 오늘의 주요 이슈</td><td>{update_time}</td><td>MarketWatch · Reuters RSS</td><td>실시간 피드</td></tr>
      <tr><td>🌍 지정학적 이슈</td><td>{update_time}</td><td>Reuters · CNN · BBC RSS</td><td>실시간 피드</td></tr>
      <tr><td>🏢 기업별 주요 이슈</td><td>{etf_fetch_time}</td><td>Finviz 종목별 뉴스</td><td>당일 최신순</td></tr>
      <tr><td>🤖 시황 요약 / 분석</td><td>{update_time}</td><td>Google Gemini {GEMINI_MODEL}</td><td>AI 생성</td></tr>
    </tbody>
  </table>
</div>

{render_hot_topics_section(hot_topics or [])}
{render_x_topics_section(x_topics or [])}

<!-- ── 주요 지수 ── -->
<div class="section">
  <div class="section-header"><div class="bar"></div><h2>주요 지수</h2></div>
  <div class="card">
    <table class="data-table">
      <thead>
        <tr>
          <th>종목명</th><th>종가</th>
          <th>일간</th><th>1주</th><th>1개월</th><th>YTD</th><th>1년</th>
        </tr>
      </thead>
      <tbody>{index_cards_html}</tbody>
    </table>
  </div>
</div>

<!-- ── 글로벌 매크로 ── -->
<div class="section">
  <div class="section-header"><div class="bar"></div><h2>글로벌 매크로</h2></div>
  <div class="macro-row">{macro_cards_html}</div>
  <div class="spark-wrap">
    <div class="spark-title">S&amp;P 500 — 최근 7거래일 추세</div>
    <canvas id="sparkChart" class="spark-canvas"></canvas>
  </div>
</div>

<!-- ── 원자재 ── -->
<div class="section">
  <div class="section-header"><div class="bar"></div><h2>원자재</h2></div>
  <div class="card">
    <table class="data-table">
      <thead>
        <tr>
          <th>종목명</th>
          <th>일간</th><th>1주</th><th>1개월</th><th>YTD</th><th>1년</th>
        </tr>
      </thead>
      <tbody>{commodity_rows_html}</tbody>
    </table>
  </div>
</div>

<!-- ── S&P 500 섹터 히트맵 ── -->
<div class="section">
  <div class="section-header"><div class="bar"></div><h2>S&amp;P 500 섹터 히트맵</h2></div>
  <div class="card" style="padding:0;overflow:hidden;min-height:500px">
    <div class="tradingview-widget-container" style="height:500px">
      <div class="tradingview-widget-container__widget" style="height:500px"></div>
      <script type="text/javascript" src="https://s3.tradingview.com/external-embedding/embed-widget-stock-heatmap.js" async>
      {{
        "exchanges": [],
        "dataSource": "SPX500",
        "grouping": "sector",
        "blockSize": "market_cap_basic",
        "blockColor": "change",
        "locale": "en",
        "colorTheme": "light",
        "hasTopBar": false,
        "isDataSetEnabled": false,
        "isZoomEnabled": true,
        "hasSymbolTooltip": true,
        "isMonoSize": false,
        "width": "100%",
        "height": "500"
      }}
      </script>
    </div>
  </div>
</div>

<!-- ── 채권 금리 ── -->
<div class="section">
  <div class="section-header"><div class="bar"></div><h2>미국 국채 금리</h2></div>
  <div class="card">
    <div class="yield-grid">{yield_rows}</div>
  </div>
</div>

<!-- ── 공포탐욕 지수 ── -->
<div class="section">
  <div class="section-header"><div class="bar"></div><h2>공포탐욕 지수</h2></div>
  <div class="card">
    <div class="fg-layout">
      <div class="gauge-wrap">
        <svg class="gauge-svg" viewBox="0 0 180 100">
          <!-- 배경 반원 구간 -->
          <path d="M10,90 A80,80 0 0,1 50,17.6" fill="none" stroke="#e74c3c" stroke-width="14" stroke-linecap="butt"/>
          <path d="M50,17.6 A80,80 0 0,1 90,10" fill="none" stroke="#e67e22" stroke-width="14" stroke-linecap="butt"/>
          <path d="M90,10 A80,80 0 0,1 115,14.6" fill="none" stroke="#aaaaaa" stroke-width="14" stroke-linecap="butt"/>
          <path d="M115,14.6 A80,80 0 0,1 153,35" fill="none" stroke="#27ae60" stroke-width="14" stroke-linecap="butt"/>
          <path d="M153,35 A80,80 0 0,1 170,90" fill="none" stroke="#16a085" stroke-width="14" stroke-linecap="butt"/>
          <!-- 바늘 -->
          <line id="fg-needle" x1="90" y1="90"
                x2="{90 + 60 * math.cos(math.radians(fg_needle_deg)):.1f}"
                y2="{90 + 60 * math.sin(math.radians(fg_needle_deg)):.1f}"
                stroke="#222" stroke-width="3" stroke-linecap="round"/>
          <circle cx="90" cy="90" r="6" fill="#222"/>
        </svg>
        <div id="fg-score-val" class="fg-score-big" style="color:{fg_color}">{fg_score if fg_score is not None else '?'}</div>
        <div id="fg-rating-val" class="fg-rating-label">{fg_rating}</div>
      </div>
      <table class="fg-legend">
        <tbody>{fg_legend_rows}</tbody>
      </table>
    </div>
  </div>
</div>
<script>
(function() {{
  var ROW_IDS = ['fg-row-extreme-fear','fg-row-fear','fg-row-neutral','fg-row-greed','fg-row-extreme-greed'];
  function getColor(s) {{ return s >= 60 ? '#4ade80' : (s <= 40 ? '#f87171' : '#facc15'); }}
  function getRowId(s) {{
    if (s <= 24) return 'fg-row-extreme-fear';
    if (s <= 44) return 'fg-row-fear';
    if (s <= 55) return 'fg-row-neutral';
    if (s <= 74) return 'fg-row-greed';
    return 'fg-row-extreme-greed';
  }}
  function applyFG(score, rating) {{
    var deg = -90 + (score / 100) * 180;
    var rad = deg * Math.PI / 180;
    var x2 = (90 + 60 * Math.cos(rad)).toFixed(1);
    var y2 = (90 + 60 * Math.sin(rad)).toFixed(1);
    var needle = document.getElementById('fg-needle');
    if (needle) {{ needle.setAttribute('x2', x2); needle.setAttribute('y2', y2); }}
    var scoreEl = document.getElementById('fg-score-val');
    if (scoreEl) {{ scoreEl.textContent = score; scoreEl.style.color = getColor(score); }}
    var ratingEl = document.getElementById('fg-rating-val');
    if (ratingEl) ratingEl.textContent = rating;
    var activeId = getRowId(score);
    ROW_IDS.forEach(function(id) {{
      var row = document.getElementById(id);
      if (!row) return;
      var td = row.querySelector('td');
      td.style.fontWeight = (id === activeId) ? '900' : '700';
    }});
  }}
  fetch('https://production.dataviz.cnn.io/index/fearandgreed/graphdata')
    .then(function(r) {{ return r.json(); }})
    .then(function(data) {{
      var fg = data && data.fear_and_greed;
      if (!fg) return;
      var score = Math.round(fg.score);
      var rating = (fg.rating || '').replace(/_/g, ' ').replace(/\b\w/g, function(c) {{ return c.toUpperCase(); }});
      applyFG(score, rating);
    }})
    .catch(function() {{}});
}})();
</script>

<!-- ── 섹터 동향 ── -->
<div class="section">
  <div class="section-header"><div class="bar"></div><h2>섹터 동향</h2></div>
  <div class="card">
    <div class="sector-grid">
      <div class="sector-col">
        <h3>▲ 상승 상위</h3>
        <ul>{gainers_html}</ul>
      </div>
      <div class="sector-col">
        <h3>▼ 하락 상위</h3>
        <ul>{losers_html}</ul>
      </div>
    </div>
  </div>
</div>

<!-- ── 주요 이슈 ── -->
<div class="section">
  <div class="section-header"><div class="bar"></div><h2>오늘의 주요 이슈</h2></div>
  <ul class="issues-list">{issues_html}</ul>
</div>

<!-- ── 애널리스트 총평 ── -->
<div class="section">
  <div class="section-header"><div class="bar"></div><h2>시황 요약</h2></div>
  <div class="analyst-box">{analyst_comment}</div>
</div>

<!-- ── ETF 테이블 ── -->
<div class="section">
  <div class="section-header"><div class="bar"></div><h2>LeverageShares x2 ETF — 수익률 TOP 20</h2></div>
  <div class="card" style="padding:0;overflow:hidden">
    <table class="data-table">
      <thead>
        <tr>
          <th style="text-align:center;width:36px">#</th>
          <th style="text-align:left">ETF</th>
          <th style="text-align:left">ETF명</th>
          <th>ETF 현재가</th>
          <th>ETF 수익률</th>
          <th style="text-align:left">기초자산</th>
          <th>기초자산 등락</th>
        </tr>
      </thead>
      <tbody>{etf_rows_html}</tbody>
    </table>
  </div>
</div>

<!-- ── 경제 캘린더 ── -->
<div class="section">
  <div class="section-header"><div class="bar"></div><h2>경제 캘린더 (1개월 이내)</h2></div>
  <div class="card" style="padding:0;overflow:hidden">
    <table class="data-table">
      <thead><tr><th style="text-align:left">날짜</th><th style="text-align:left">시간(ET)</th><th style="text-align:left">이벤트</th><th>예측</th><th>이전</th></tr></thead>
      <tbody>{econ_html if econ_html else '<tr><td colspan="5" style="text-align:center;color:#888;padding:16px">[데이터 생성 중]</td></tr>'}</tbody>
    </table>
  </div>
</div>

<!-- ── 실적발표 일정 ── -->
<div class="section">
  <div class="section-header"><div class="bar"></div><h2>실적발표 일정 (1개월 이내)</h2></div>
  <div class="card" style="padding:0;overflow:hidden">
    <table class="data-table">
      <thead><tr><th style="text-align:left">날짜</th><th style="text-align:left">기업 (티커)</th><th>EPS 예측</th><th>매출 예측</th><th>발표 시간</th></tr></thead>
      <tbody>{earn_html if earn_html else '<tr><td colspan="5" style="text-align:center;color:#888;padding:16px">[데이터 생성 중]</td></tr>'}</tbody>
    </table>
  </div>
</div>

<!-- ── 지정학적 이슈 ── -->
<div class="section">
  <div class="section-header"><div class="bar"></div><h2>지정학적 이슈</h2></div>
  <ul class="issues-list">{geo_html}</ul>
</div>

<!-- ── 기업별 주요 이슈 ── -->
<div class="section">
  <div class="section-header"><div class="bar"></div><h2>기업별 주요 이슈</h2></div>
  <ul class="issues-list">{corp_html}</ul>
</div>

<footer id="today-footer-date">Generated by MarketBrief · {date}</footer>

<script>
(function() {{
  const labels = {spark_labels};
  const values = {spark_values};
  if (!values.length) return;
  const ctx = document.getElementById('sparkChart');
  if (!ctx) return;
  new Chart(ctx, {{
    type: 'line',
    data: {{
      labels: labels,
      datasets: [{{
        data: values,
        borderColor: '{spark_color}',
        backgroundColor: '{spark_color}33',
        borderWidth: 2,
        pointRadius: 3,
        pointBackgroundColor: '{spark_color}',
        fill: true,
        tension: 0.3,
      }}]
    }},
    options: {{
      responsive: true,
      maintainAspectRatio: false,
      plugins: {{ legend: {{ display: false }} }},
      scales: {{
        x: {{ grid: {{ color: '#f0f0f0' }}, ticks: {{ color: '#999', font: {{ size: 10 }} }} }},
        y: {{ grid: {{ color: '#f0f0f0' }}, ticks: {{ color: '#999', font: {{ size: 10 }} }} }},
      }}
    }}
  }});
}})();
</script>
</body>
</html>"""

    return html


# ============================================================
# 7. EMAIL SENDER
# ============================================================

def send_email(
    brief_date: str,
    html_body:  str,
    recipients: list[str],
) -> bool:
    """
    Gmail SMTP로 HTML 시황 브리핑 발송.

    .env에 아래 항목이 필요합니다:
        EMAIL_SENDER    = your_gmail@gmail.com
        EMAIL_PASSWORD  = Gmail 앱 비밀번호 (16자리)
        EMAIL_RECIPIENTS= a@x.com,b@x.com  (쉼표 구분)
    """
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text      import MIMEText

    sender   = os.getenv("EMAIL_SENDER", "")
    password = os.getenv("EMAIL_PASSWORD", "")

    if not sender or not password:
        print("  ⚠️  EMAIL_SENDER / EMAIL_PASSWORD 미설정 — 이메일 발송 건너뜀")
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"📊 {brief_date} 미증시 시황 — MarketBrief"
    msg["From"]    = sender
    msg["To"]      = ", ".join(recipients)
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(sender, password)
            smtp.sendmail(sender, recipients, msg.as_string())
        print(f"  ✉️  이메일 발송 완료 → {', '.join(recipients)}")
        return True
    except Exception as e:
        print(f"  ⚠️  이메일 발송 실패: {e}")
        return False


# ============================================================
# 8. SLACK WEBHOOK NOTIFICATION
# ============================================================

def send_slack(market_data: dict, brief_summary: str) -> bool:
    """
    Slack Incoming Webhook으로 시황 요약 전송.

    .env에 아래 항목이 필요합니다:
        SLACK_WEBHOOK_URL = https://hooks.slack.com/services/XXX/YYY/ZZZ
    """
    import urllib.request

    webhook_url = os.getenv("SLACK_WEBHOOK_URL", "")
    if not webhook_url:
        print("  ⚠️  SLACK_WEBHOOK_URL 미설정 — Slack 발송 건너뜀")
        return False

    date    = market_data["date"]
    indices = market_data["indices"]
    fg      = market_data.get("fear_greed", {})
    yields  = market_data.get("yields", {})

    def idx_line(name, info):
        pct = info.get("change_pct", "")
        icon = "📈" if "+" in pct else ("📉" if "-" in pct else "➡️")
        return f"{icon} *{name}* {info.get('close','')} `{pct}`"

    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"📊 {date} 미국 증시 시황"},
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": idx_line("S&P 500", indices.get("SP500",  {}))},
                {"type": "mrkdwn", "text": idx_line("NASDAQ",  indices.get("NASDAQ", {}))},
                {"type": "mrkdwn", "text": idx_line("DOW",     indices.get("DOW",    {}))},
                {"type": "mrkdwn", "text": idx_line("VIX",     indices.get("VIX",    {}))},
            ],
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*10Y 금리* `{yields.get('10Y', '?')}`"},
                {"type": "mrkdwn", "text": f"*Fear & Greed* `{fg.get('score','?')} — {fg.get('rating','?')}`"},
            ],
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*애널리스트 총평*\n{brief_summary[:300]}{'...' if len(brief_summary) > 300 else ''}",
            },
        },
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f"MarketBrief · {market_data['update_time']}"}],
        },
    ]

    payload = json.dumps({"blocks": blocks}).encode("utf-8")
    req = urllib.request.Request(
        webhook_url,
        data    = payload,
        headers = {"Content-Type": "application/json"},
        method  = "POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status == 200:
                print("  💬 Slack 전송 완료")
                return True
            print(f"  ⚠️  Slack 응답: {resp.status}")
            return False
    except Exception as e:
        print(f"  ⚠️  Slack 전송 실패: {e}")
        return False


# ============================================================
# 9. PUSH NOTIFICATION PAYLOAD
# ============================================================

def build_push_payload(brief_date: str, send_at_kst: str = "06:00") -> dict:
    """FCM / APNs 푸시 알림 페이로드 생성"""
    return {
        "title":       f"📊 {brief_date} 미증시 시황 업데이트",
        "body":        "오늘 꼭 알아야 할 미국 증시 핵심, 지금 확인하세요.",
        "deep_link":   f"marketbrief://brief/{brief_date}",
        "send_at_kst": send_at_kst,
    }


# ============================================================
# 10. TELEGRAM NOTIFICATION
# ============================================================

def send_telegram(market_data: dict, brief_summary: str) -> bool:
    """
    Telegram Bot API로 시황 요약 메시지 전송.

    .env에 아래 항목이 필요합니다:
        TELEGRAM_BOT_TOKEN = 123456789:ABCdef...  (BotFather에서 발급)
        TELEGRAM_CHAT_ID   = -100123456789        (채널 ID 또는 그룹 ID)

    Telegram Bot 설정:
        1. BotFather (@BotFather) 에게 /newbot 명령
        2. 발급된 TOKEN을 .env에 저장
        3. 봇을 채널/그룹에 관리자로 초대
        4. 채널 ID: https://api.telegram.org/bot{TOKEN}/getUpdates 로 확인
    """
    import urllib.request, urllib.parse

    token   = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID",   "")

    if not token or not chat_id:
        print("  ⚠️  TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 미설정 — Telegram 건너뜀")
        return False

    date    = market_data["date"]
    indices = market_data["indices"]
    macro   = market_data.get("macro", {})
    fg      = market_data.get("fear_greed", {})

    def idx_line(label, info):
        pct  = info.get("change_pct", "")
        icon = "📈" if "+" in pct else ("📉" if "-" in pct else "➡️")
        return f"{icon} *{label}*: {info.get('close','')} `{pct}`"

    lines = [
        f"📊 *{date} 미국 증시 시황*",
        "",
        idx_line("S\\&P 500", indices.get("SP500",  {})),
        idx_line("NASDAQ",    indices.get("NASDAQ", {})),
        idx_line("DOW",       indices.get("DOW",    {})),
        idx_line("VIX",       indices.get("VIX",    {})),
        "",
        f"💵 *DXY*: {macro.get('DXY','?')}",
        f"🥇 *Gold*: {macro.get('Gold','?')}",
        f"🛢 *Oil*: {macro.get('Oil','?')}",
        "",
        f"😱 *Fear \\& Greed*: {fg.get('score','?')} — {fg.get('rating','?')}",
        "",
        "─────────────────",
        f"💬 {brief_summary[:280]}{'…' if len(brief_summary) > 280 else ''}",
    ]
    text = "\n".join(lines)

    url     = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({
        "chat_id":    chat_id,
        "text":       text,
        "parse_mode": "MarkdownV2",
    }).encode("utf-8")

    req = urllib.request.Request(
        url,
        data    = payload,
        headers = {"Content-Type": "application/json"},
        method  = "POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode())
            if result.get("ok"):
                print("  📱 Telegram 전송 완료")
                return True
            print(f"  ⚠️  Telegram 오류: {result}")
            return False
    except Exception as e:
        print(f"  ⚠️  Telegram 전송 실패: {e}")
        return False


# ============================================================
# 11. MOCK DATA (--test 모드용)
# ============================================================

def build_mock_market_data() -> dict:
    """
    네트워크 없이 파이프라인 전체를 검증하기 위한 샘플 데이터.
    실제 데이터 형식과 동일한 구조를 유지합니다.
    """
    # mock 데이터: 수익률 내림차순 정렬
    _mock_raw = [
        ("COIG", "2x Long COIN Daily ETF",    698_341, "+6.22%", "6.89"),
        ("PLTG", "2x Long PLTR Daily ETF",    754_890, "+5.07%", "16.17"),
        ("NVDG", "2x Long NVDA Daily ETF",  1_234_567, "+4.82%", "14.32"),
        ("HOOG", "2x Long HOOD Daily ETF",    543_210, "+3.55%", "17.33"),
        ("NIOG", "2x Long NIO Daily ETF",     210_987, "+3.14%", "21.61"),
        ("XPEG", "2x Long XPEV Daily ETF",    176_543, "+2.77%", "9.70"),
        ("AVGG", "2x Long AVGO Daily ETF",    612_004, "+2.41%", "20.91"),
        ("DUOG", "2x Long DUOL Daily ETF",    198_765, "+1.99%", "3.24"),
        ("ARMG", "2x Long ARM Daily ETF",     589_770, "+1.68%", "9.37"),
        ("SPOG", "2x Long SPOT Daily ETF",    342_001, "+1.12%", "7.58"),
        ("CRMG", "2x Long CRM Daily ETF",     234_567, "+0.95%", "6.17"),
        ("TSMG", "2x Long TSM Daily ETF",     432_109, "+0.91%", "28.05"),
        ("NETG", "2x Long NET Daily ETF",     387_654, "+0.44%", "12.74"),
        ("UNHG", "2x Long UNH Daily ETF",     132_109, "-0.33%", "11.00"),
        ("PANG", "2x Long PANW Daily ETF",    265_432, "-0.78%", "7.89"),
        ("AMDG", "2x Long AMD Daily ETF",     876_123, "+1.33%", "24.16"),
        ("LULG", "2x Long LULU Daily ETF",    154_321, "-1.05%", "12.62"),
        ("PYPG", "2x Long PYPL Daily ETF",    298_543, "-1.44%", "5.99"),
        ("TSLG", "2x Long TSLA Daily ETF",    987_432, "-2.14%", "5.36"),
        ("SNAG", "2x Long SNAP Daily ETF",    498_765, "-4.22%", "5.06"),
    ]
    mock_etf = [
        {"rank": i, "ticker": t, "name": n, "volume": f"{v:,}", "return_1d": r, "price": p}
        for i, (t, n, v, r, p) in enumerate(_mock_raw, 1)
    ]
    mock_commodities = [
        {"name": "금",       "flag": "🥇", "d1": "-0.16%", "w1": "+3.78%", "m1": "+6.82%", "ytd": "+5.58%",  "y1": "+18.23%"},
        {"name": "은",       "flag": "🥈", "d1": "-0.30%", "w1": "+20.23%","m1": "+46.75%","ytd": "+19.76%", "y1": "+35.12%"},
        {"name": "구리",     "flag": "🇺🇸", "d1": "-0.02%", "w1": "+4.11%", "m1": "+12.74%","ytd": "+7.38%",  "y1": "+12.44%"},
        {"name": "백금",     "flag": "⬜",  "d1": "-0.15%", "w1": "+6.20%", "m1": "+32.68%","ytd": "+17.86%", "y1": "+8.91%"},
        {"name": "브렌트유", "flag": "🇬🇧", "d1": "-0.52%", "w1": "+8.81%", "m1": "+7.73%", "ytd": "+7.21%",  "y1": "-18.33%"},
        {"name": "WTI유",   "flag": "🇺🇸", "d1": "-4.17%", "w1": "+8.70%", "m1": "+7.11%", "ytd": "+5.99%",  "y1": "-19.44%"},
        {"name": "천연가스", "flag": "🇺🇸", "d1": "+0.06%", "w1": "-11.80%","m1": "-22.51%","ytd": "-15.65%", "y1": "+42.11%"},
        {"name": "옥수수",   "flag": "🇺🇸", "d1": "-0.06%", "w1": "-5.54%", "m1": "-4.04%", "ytd": "-4.15%",  "y1": "-12.33%"},
        {"name": "소맥",     "flag": "🇺🇸", "d1": "-0.10%", "w1": "-0.97%", "m1": "-1.49%", "ytd": "+1.18%",  "y1": "-8.77%"},
    ]
    return {
        "date":           datetime.datetime.now(KST).strftime("%Y-%m-%d"),
        "close_date":     (datetime.datetime.now(KST) - datetime.timedelta(days=1)).strftime("%Y-%m-%d"),
        "update_time":    datetime.datetime.now(KST).strftime("%H:%M KST"),
        "etf_fetch_time": datetime.datetime.now(KST).strftime("%Y-%m-%d %H:%M KST"),
        "indices": {
            "SP500":  {"close": "5,074.08", "change_pct": "-1.97%", "change_1w": "-3.21%", "change_1m": "-6.11%", "change_ytd": "-13.73%", "change_1y": "+1.51%"},
            "NASDAQ": {"close": "15,587.79","change_pct": "-2.83%", "change_1w": "-5.12%", "change_1m": "-9.44%", "change_ytd": "-19.57%", "change_1y": "+1.62%"},
            "DOW":    {"close": "38,314.86","change_pct": "-1.56%", "change_1w": "-2.88%", "change_1m": "-4.78%", "change_ytd": "-9.02%",  "change_1y": "+1.51%"},
            "VIX":    {"close": "30.02",    "change_pct": "+38.76%","change_1w": "+89.44%","change_1m": "+89.44%","change_ytd": "+89.44%", "change_1y": "+1.52%"},
        },
        "yields": {
            "2Y":  "3.92% (+0.04bp)",
            "10Y": "4.01% (-0.12bp)",
            "30Y": "4.34% (-0.08bp)",
        },
        "fear_greed": {"score": 18, "rating": "Extreme Fear"},
        "sector_heatmap": {
            "top_gainers": ["Utilities +0.82%", "Consumer Staples +0.31%", "Health Care +0.10%"],
            "top_losers":  ["Technology -3.52%", "Consumer Disc. -3.01%", "Communication -2.74%"],
        },
        "key_issues": [
            {"title": "Fed officials signal caution on rate cuts amid sticky inflation data", "url": "https://www.marketwatch.com/story/fed-officials-signal-caution-on-rate-cuts"},
            {"title": "Trump tariffs: 10% baseline on all imports, China faces 54% total levy", "url": "https://www.reuters.com/business/trump-tariffs-baseline-imports"},
            {"title": "Jobs report beats expectations: +228K vs +140K forecast", "url": "https://www.marketwatch.com/story/jobs-report-beats-expectations"},
            {"title": "Apple warns of $900M cost impact from new tariffs on China-made devices", "url": "https://www.reuters.com/technology/apple-warns-900m-tariff-cost"},
            {"title": "Treasury 10Y yield dips below 4% for first time since October", "url": "https://www.marketwatch.com/story/treasury-yield-below-4-percent"},
        ],
        "geo_issues": [
            {"title": "Iran-US tensions escalate as military strikes remain on table", "url": "https://www.reuters.com/world/middle-east/iran-us-tensions-escalate"},
            {"title": "Russia-Ukraine war: EU considers new sanctions package", "url": "https://www.reuters.com/world/europe/russia-ukraine-eu-sanctions"},
            {"title": "South China Sea: US Navy patrol draws Beijing warning", "url": "https://www.reuters.com/world/asia-pacific/south-china-sea-us-navy"},
            {"title": "Middle East ceasefire talks stall amid renewed hostilities", "url": "https://www.reuters.com/world/middle-east/ceasefire-talks-stall"},
            {"title": "North Korea missile launch prompts US-Japan security talks", "url": "https://www.reuters.com/world/asia-pacific/north-korea-missile-launch"},
        ],
        "corp_issues": [
            {"title": "NVDA: Blackwell GPU demand surges, analyst raises price target to $180", "url": "https://www.marketwatch.com/story/nvidia-blackwell-gpu-demand"},
            {"title": "TSLA: Q1 deliveries miss estimates; Musk refocuses on Tesla", "url": "https://www.reuters.com/business/autos-transportation/tesla-q1-deliveries-miss"},
            {"title": "AAPL: tariff exposure estimate raised to $900M for China devices", "url": "https://www.marketwatch.com/story/apple-tariff-exposure-900m"},
            {"title": "META: Llama 4 launch accelerates AI assistant rollout", "url": "https://www.reuters.com/technology/meta-llama-4-launch"},
            {"title": "AMZN: AWS revenue growth reaccelerates to 17% YoY", "url": "https://www.marketwatch.com/story/amazon-aws-revenue-growth"},
        ],
        "macro": {
            "DXY":  "102.34 (-0.41%)",
            "Gold": "3,038.20 $/oz (+1.23%)",
            "Oil":  "61.99 $/bbl (-4.17%)",
        },
        "sparkline_sp500": [5312.0, 5283.4, 5195.1, 5148.0, 5074.1, 5074.1, 5074.1],
        "commodities":        mock_commodities,
        "econ_calendar":      [],
        "earn_calendar":      [],
        "underlying_stocks":  {
            "NVDA": {"price": "106.63", "change": "-4.77%", "volume": "312.45M"},
            "TSLA": {"price": "228.81", "change": "-5.47%", "volume": "134.21M"},
            "AMD":  {"price": "88.42",  "change": "-3.22%", "volume": "89.33M"},
            "PLTR": {"price": "91.23",  "change": "+2.14%", "volume": "145.67M"},
            "COIN": {"price": "184.55", "change": "+3.88%", "volume": "22.44M"},
            "AVGO": {"price": "168.22", "change": "-1.98%", "volume": "31.12M"},
            "ARM":  {"price": "99.45",  "change": "-2.33%", "volume": "18.77M"},
            "HOOD": {"price": "41.22",  "change": "+1.55%", "volume": "55.32M"},
            "SNAP": {"price": "7.88",   "change": "-6.11%", "volume": "43.21M"},
            "TSM":  {"price": "158.44", "change": "+0.44%", "volume": "14.55M"},
        },
        "leverage_etf_top20": mock_etf,
    }


# ============================================================
# 11. ARCHIVE INDEX GENERATOR
# ============================================================

def generate_archive_index(out_dir: Path) -> None:
    """
    output/ 폴더의 data_*.json 파일을 스캔해서
    output/index.html (브리핑 아카이브 목록)을 생성합니다.
    AM/PM 두 파일을 모두 표시합니다.
    """
    data_files = sorted(out_dir.glob("data_*.json"), reverse=True)
    if not data_files:
        return

    # 가장 최근 날짜의 오늘의 화제 TOP 10 (AM 브리핑에서만 생성됨) — 있으면 표시, 없으면 생략.
    # 화제 검색은 AM 실행에서만 도므로, 오늘 PM만 실행된 날은 정확히 오늘 날짜의
    # 파일이 없을 수 있음 — load_latest_hot_topics가 가장 최근 파일로 대체.
    hot_topics_html = ""
    try:
        latest_date     = json.loads(data_files[0].read_text(encoding="utf-8")).get("date", "")
        hot_topics_html = render_hot_topics_section(load_latest_hot_topics(out_dir, latest_date))
    except Exception:
        hot_topics_html = ""

    rows_html = ""
    for path in data_files:
        try:
            data      = json.loads(path.read_text(encoding="utf-8"))
            date      = data.get("date", "")
            stem      = path.stem.replace("data_", "")   # e.g. 2026-04-08_AM
            sp500     = data.get("indices", {}).get("SP500", {})
            vix       = data.get("indices", {}).get("VIX",   {})
            fg        = data.get("fear_greed", {})

            # AM/PM 배지 표시
            if stem.endswith("_AM"):
                badge = "<span style='background:#1a56db;color:#fff;font-size:11px;padding:1px 6px;border-radius:4px'>오전</span>"
            elif stem.endswith("_PM"):
                badge = "<span style='background:#e65100;color:#fff;font-size:11px;padding:1px 6px;border-radius:4px'>오후</span>"
            else:
                badge = ""

            brief_file = f"brief_{stem}.html"
            exists     = (out_dir / brief_file).exists()
            label      = date or stem
            link       = f'<a href="{brief_file}">{label}</a> {badge}' if exists else f"{label} {badge}"

            sp_pct   = sp500.get("change_pct", "-")
            sp_color = "#4ade80" if "+" in sp_pct else ("#f87171" if "-" in sp_pct else "#94a3b8")
            vix_val  = vix.get("close", "-")
            fg_score = fg.get("score", "-")
            fg_rat   = fg.get("rating", "-")

            rows_html += f"""
        <tr>
          <td>{link}</td>
          <td>{sp500.get('close', '-')}</td>
          <td style="color:{sp_color};font-weight:600">{sp_pct}</td>
          <td>{vix_val}</td>
          <td>{fg_score} <span style="color:#64748b;font-size:.75rem">({fg_rat})</span></td>
          <td>{data.get('update_time', '-')}</td>
        </tr>"""
        except Exception:
            continue

    html = f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>MarketBrief Archive</title>
<style>
  * {{ box-sizing:border-box; margin:0; padding:0; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    background:#0f0f1a; color:#e2e8f0;
    padding:32px; max-width:860px; margin:0 auto;
  }}
  h1 {{ font-size:1.4rem; margin-bottom:6px; }}
  .sub {{ color:#64748b; font-size:.85rem; margin-bottom:28px; }}
  table {{ width:100%; border-collapse:collapse; font-size:.85rem; }}
  th {{
    background:#1e1e2e; padding:10px 12px;
    text-align:left; color:#64748b; font-weight:600;
    border-bottom:1px solid #2d2d44;
  }}
  td {{ padding:10px 12px; border-bottom:1px solid #1a1a2e; }}
  tr:hover td {{ background:#1e1e2e; }}
  a {{ color:#93c5fd; text-decoration:none; }}
  a:hover {{ text-decoration:underline; }}
  footer {{ margin-top:32px; text-align:center; font-size:.75rem; color:#334155; }}
  .hot-topics-section {{ margin-bottom:32px; }}
  .hot-topics-section h2 {{ font-size:1.1rem; margin-bottom:4px; }}
  .hot-topics-sub {{ color:#64748b; font-size:.8rem; margin-bottom:16px; }}
  .hot-topics-grid {{ display:grid; grid-template-columns:1fr; gap:10px; }}
  .hot-topic-card {{
    background:#1e1e2e; border:1px solid #2d2d44; border-radius:8px;
    padding:12px 14px; position:relative;
  }}
  .hot-topic-rank {{ color:#64748b; font-size:.8rem; font-weight:700; margin-right:6px; }}
  .hot-topic-format {{
    color:#fff; font-size:.7rem; padding:1px 8px; border-radius:4px;
    float:right;
  }}
  .hot-topic-title {{ font-weight:600; margin-top:4px; }}
  .hot-topic-reason {{ color:#94a3b8; font-size:.8rem; margin-top:4px; }}
  .hot-topic-src-btn {{
    display:inline-block; margin-left:8px;
    padding:1px 8px; background:#334155; color:#93c5fd !important;
    font-size:.7rem; font-weight:600; border-radius:4px;
    text-decoration:none !important; white-space:nowrap;
  }}
  .hot-topic-src-btn:hover {{ background:#475569; }}
</style>
</head>
<body>
<h1>📊 MarketBrief Archive</h1>
<p class="sub">생성된 일일 시황 브리핑 목록 — 날짜를 클릭하면 해당 브리핑으로 이동합니다.</p>
{hot_topics_html}
<table>
  <thead>
    <tr>
      <th>날짜</th><th>S&amp;P 500</th><th>등락률</th>
      <th>VIX</th><th>Fear &amp; Greed</th><th>생성 시각</th>
    </tr>
  </thead>
  <tbody>{rows_html}</tbody>
</table>
<footer>MarketBrief · Powered by Claude · 마지막 업데이트: {datetime.datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")}</footer>
</body>
</html>"""

    index_path = out_dir / "index.html"
    index_path.write_text(html, encoding="utf-8")
    print(f"📋 아카이브 인덱스 갱신: {index_path}")


# ============================================================
# 12. MAIN PIPELINE
# ============================================================

def generate_daily_brief(
    dry_run:       bool = False,
    save_output:   bool = True,
    send_email_:   bool = False,
    send_slack_:   bool = False,
    send_telegram_:bool = False,
    test_mode:     bool = False,
) -> str:
    """
    MarketBrief 일일 시황 생성 메인 파이프라인

    Args:
        dry_run:      True면 Claude API를 호출하지 않고 prompt만 출력
        save_output:  True면 output/ 폴더에 .md / .html / .json 저장
        send_email_:    True면 .env의 EMAIL_RECIPIENTS로 HTML 브리핑 발송
        send_slack_:    True면 .env의 SLACK_WEBHOOK_URL로 요약 전송
        send_telegram_: True면 .env의 TELEGRAM_BOT_TOKEN / CHAT_ID로 전송
        test_mode:      True면 mock 데이터 사용 (네트워크·API 불필요)
    """
    print("=" * 60)
    if test_mode:
        print("🧪 MarketBrief [TEST MODE] 시작")
    else:
        print("🗞️  MarketBrief 일일 시황 생성 시작")
    print("=" * 60)

    saved_html_path = None  # 저장된 HTML 경로 (--open 용)

    # 실행 시각 기준 AM(오전) / PM(오후) 구분 — 이후 여러 Step에서 재사용
    current_hour = datetime.datetime.now(KST).hour
    time_suffix  = "AM" if current_hour < 12 else "PM"

    # Step 1 — 시장 데이터 수집
    if test_mode:
        print("🧪 Mock 데이터 사용 (네트워크 미호출)")
        market_data = build_mock_market_data()
    else:
        fetcher     = MarketDataFetcher()
        market_data = fetcher.collect_all()

    # Step 2 — 뉴스 + 캘린더 수집
    if test_mode:
        pass  # mock_data에 이미 key_issues 포함
    else:
        nf = NewsFetcher()
        print("📰 주요 뉴스 수집 중...")
        market_data["key_issues"]  = nf.fetch()
        print("🌍 지정학 뉴스 수집 중...")
        geo_items = nf.fetch_geo()
        print("🌐 지정학 뉴스 제목 한국어 번역 중...")
        translate_titles_ko(geo_items)
        market_data["geo_issues"] = geo_items
        print("🏢 기업 뉴스 수집 중 (Finviz + Bloomberg 링크)...")
        corp_tickers = list(ETF_UNDERLYING.values())
        raw_corp = FinvizFetcher().fetch_corp_news(corp_tickers, max_per_ticker=1)
        # Finviz 실제 기사 URL 유지, 출처 표기
        for item in raw_corp:
            ticker = item.get("ticker", "")
            if ticker and not item.get("url"):
                item["url"] = f"https://finviz.com/quote.ashx?t={ticker}"
            item["source"] = "FinViz"
        print("🌐 기업 뉴스 제목 한국어 번역 중...")
        translate_titles_ko(raw_corp)
        market_data["corp_issues"] = raw_corp

        cal = CalendarFetcher()
        print("📅 경제 캘린더 수집 중...")
        market_data["econ_calendar"]    = cal.fetch_economic()
        print("📊 실적발표 일정 수집 중...")
        market_data["earn_calendar"]    = cal.fetch_earnings()

    for i, issue in enumerate(market_data["key_issues"], 1):
        title = issue["title"] if isinstance(issue, dict) else issue
        print(f"  {i}. {title[:80]}{'...' if len(title) > 80 else ''}")

    # Step 2.5 — 오늘의 화제 TOP 10 (숏츠/카드뉴스 소재, AM 브리핑에서만 실행)
    # google_search grounding으로 실시간 검색 — 실패해도 브리핑 생성 자체에는 영향 없음
    # (실패 시 output/index.html 섹션만 생략됨)
    if save_output and not test_mode:
        try:
            print("🔥 오늘의 화제 TOP10 검색 중 (숏츠/카드뉴스 소재)...")
            hot_topics = fetch_hot_topics(market_data)
            out_dir = Path("output")
            out_dir.mkdir(exist_ok=True)
            hot_topics_path = out_dir / f"hot_topics_{market_data['date']}.json"
            hot_topics_path.write_text(
                json.dumps(hot_topics, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print(f"✅ 화제 TOP10 저장: {hot_topics_path}")
        except Exception as e:
            print(f"⚠️  화제 TOP10 추출 실패 (index.html 섹션 생략): {e}")
    # Step 2.6 — X 화제 토픽 수집
    if save_output and not test_mode:
        try:
            print("🐦 X 화제 토픽 수집 중...")
            _x = fetch_x_topics()
            if _x:
                print(f"✅ X 토픽 저장: {save_x_topics(Path('output'), market_data['date'], _x)}")
        except Exception as e:
            print(f"⚠️  X 토픽 수집 실패 (섹션 생략): {e}")
    # Step 3 — 프롬프트 빌드
    print("\n📝 프롬프트 생성 중...")
    prompt = build_prompt(market_data)

    if dry_run:
        print("\n[DRY RUN] 생성된 프롬프트:\n")
        print(prompt)
        print("\n[DRY RUN] Claude API 미호출. 종료.")
        return prompt

    # Step 4 — Claude API 호출
    print("🤖 Claude API 호출 중...")
    response = call_gemini(prompt=prompt, system=SYSTEM_PERSONA)

    # Step 5 — 출력 검증
    validation = validate_output(response)
    if not validation["is_valid"]:
        print(f"\n⚠️  누락된 섹션 감지: {validation['missing_sections']}")
    else:
        print("✅ 출력 검증 통과")

    # Step 6 — HTML 생성
    print("🎨 HTML 리포트 생성 중...")
    hot_topics_for_html = load_latest_hot_topics(Path("output"), market_data["date"])
    x_topics_for_html   = load_latest_x_topics(Path("output"), market_data["date"])
    html_report = build_html_report(market_data, response, hot_topics=hot_topics_for_html, x_topics=x_topics_for_html)

    # Step 7 — 파일 저장
    if save_output:
        out_dir    = Path("output")
        out_dir.mkdir(exist_ok=True)
        brief_date = market_data["date"]
        file_key   = f"{brief_date}_{time_suffix}"   # e.g. 2026-04-08_AM

        # 시황 브리핑 마크다운
        md_path = out_dir / f"brief_{file_key}.md"
        header  = f"# MarketBrief — {brief_date} ({time_suffix})\n_생성 시각: {market_data['update_time']}_\n\n"
        md_path.write_text(header + response, encoding="utf-8")
        print(f"📄 마크다운 저장: {md_path}")

        # HTML 리포트
        html_path = out_dir / f"brief_{file_key}.html"
        html_path.write_text(html_report, encoding="utf-8")
        print(f"🌐 HTML 저장:     {html_path}")
        saved_html_path = html_path

        # 항상 brief_YYYY-MM-DD.html 도 최신으로 덮어쓰기 (링크 고정용)
        base_html_path = out_dir / f"brief_{brief_date}.html"
        base_html_path.write_text(html_report, encoding="utf-8")
        print(f"🔗 최신 파일 갱신: {base_html_path}")

        # Raw 데이터 JSON
        json_path = out_dir / f"data_{file_key}.json"
        json_path.write_text(
            json.dumps(market_data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"💾 데이터 저장:   {json_path}")

        # 푸시 알림 페이로드 JSON
        push_path = out_dir / f"push_{file_key}.json"
        push_path.write_text(
            json.dumps(build_push_payload(brief_date), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"🔔 푸시 페이로드: {push_path}")

        # 아카이브 인덱스 자동 갱신
        if CFG.get("output", {}).get("auto_update_index", True):
            generate_archive_index(out_dir)

    # Step 8 — 이메일 발송
    if send_email_:
        print("✉️  이메일 발송 중...")
        raw_recipients = os.getenv("EMAIL_RECIPIENTS", "")
        recipients = [r.strip() for r in raw_recipients.split(",") if r.strip()]
        if recipients:
            send_email(market_data["date"], html_report, recipients)
        else:
            print("  ⚠️  EMAIL_RECIPIENTS 미설정 — .env 확인")

    # 알림용 애널리스트 총평 추출 (## 5. 섹션)
    summary_lines = []
    in_section    = False
    for line in response.splitlines():
        if "## 5." in line:
            in_section = True
            continue
        if in_section:
            if line.startswith("## "):
                break
            stripped = line.strip().lstrip("→").strip()
            if stripped:
                summary_lines.append(stripped)
    brief_summary = " ".join(summary_lines)

    # Step 9 — Slack 알림
    if send_slack_:
        print("💬 Slack 알림 전송 중...")
        send_slack(market_data, brief_summary)

    # Step 10 — Telegram 알림
    if send_telegram_:
        print("📱 Telegram 알림 전송 중...")
        send_telegram(market_data, brief_summary)

    print("=" * 60)
    print("✨ MarketBrief 생성 완료")
    print("=" * 60)
    return response, saved_html_path if save_output else None


# ============================================================
# 8. CLI ENTRY POINT
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="MarketBrief — 미국 증시 일일 시황 자동 생성",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Claude API 미호출. 데이터 수집 + 프롬프트 출력만 수행.",
    )
    parser.add_argument(
        "--show-data",
        action="store_true",
        help="수집된 raw 데이터를 JSON으로 출력하고 종료.",
    )
    parser.add_argument(
        "--no-save",
        action="store_true",
        help="output/ 폴더에 파일을 저장하지 않음.",
    )
    parser.add_argument(
        "--log-file",
        action="store_true",
        help="logs/ 폴더에 로그 파일도 저장.",
    )
    parser.add_argument(
        "--email",
        action="store_true",
        help=".env의 EMAIL_RECIPIENTS로 HTML 브리핑 이메일 발송.",
    )
    parser.add_argument(
        "--slack",
        action="store_true",
        help=".env의 SLACK_WEBHOOK_URL로 시황 요약 전송.",
    )
    parser.add_argument(
        "--telegram",
        action="store_true",
        help=".env의 TELEGRAM_BOT_TOKEN / CHAT_ID로 시황 요약 전송.",
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="Mock 데이터로 전체 파이프라인 검증 (네트워크·API 키 불필요).",
    )
    parser.add_argument(
        "--index",
        action="store_true",
        help="output/index.html 아카이브 인덱스만 갱신하고 종료.",
    )
    parser.add_argument(
        "--open",
        action="store_true",
        help="생성 완료 후 HTML 리포트를 브라우저로 자동 열기.",
    )
    args = parser.parse_args()
    setup_logging(log_to_file=args.log_file)

    if args.index:
        out_dir = Path("output")
        out_dir.mkdir(exist_ok=True)
        generate_archive_index(out_dir)
        return

    if args.show_data:
        if args.test:
            print(json.dumps(build_mock_market_data(), ensure_ascii=False, indent=2))
        else:
            fetcher     = MarketDataFetcher()
            market_data = fetcher.collect_all()
            market_data["key_issues"] = NewsFetcher().fetch()
            print(json.dumps(market_data, ensure_ascii=False, indent=2))
        return

    result = generate_daily_brief(
        dry_run     = args.dry_run,
        save_output = not args.no_save,
        send_email_ = args.email,
        send_slack_     = args.slack,
        send_telegram_  = args.telegram,
        test_mode       = args.test,
    )
    brief, saved_html = (result if isinstance(result, tuple) else (result, None))

    if not args.dry_run:
        if args.open and saved_html and saved_html.exists():
            import webbrowser
            os.startfile(str(saved_html.resolve()))
            print(f"🌐 브라우저로 열기: {saved_html}")
        elif not args.open:
            print("\n" + "=" * 60)
            print(brief)


if __name__ == "__main__":
    main()
