"""
X(구 트위터) 화제 토픽 수집 모듈 — MarketBrief 연동용

marketbrief.py에서 아래 3개 함수만 가져다 씁니다:
    fetch_x_topics()        — xAI X Search로 토픽 수집
    render_x_topics_section() — HTML 섹션 생성
    load_latest_x_topics()  — 저장된 결과 로드

설정은 config.json의 "x_topics" 항목에서 읽습니다.
환경변수 XAI_API_KEY가 없거나 xai-sdk가 없으면 조용히 빈 리스트를 반환하므로,
X 수집이 실패해도 시황 브리핑 생성 자체에는 영향이 없습니다.
"""

import os
import re
import json
import datetime
from pathlib import Path

# ── 기본 설정 (config.json에 "x_topics"가 없을 때 사용) ──────────
DEFAULTS = {
    "enabled": True,
    "model": "grok-4.3",
    "topic_count": 8,
    "lookback_hours": 12,
    "min_topics": 5,
    "fallback_hours": 24,
    "use_handle_filter": True,
    "handles": [
        "FuturesnowNews",
        "financialjuice",
        "DeItaone",
        "SemiAnalysis_",
        "eWhispers",
        "Reuters",
        "SawyerMerritt",
        "KobeissiLetter",
        "StockMKTNewz",
        "FirstSquawk",
    ],
}


def _load_x_config() -> dict:
    """config.json의 x_topics 항목을 기본값과 병합해서 반환."""
    cfg_path = Path(__file__).parent / "config.json"
    if not cfg_path.exists():
        return dict(DEFAULTS)
    try:
        raw = json.loads(cfg_path.read_text(encoding="utf-8"))
        user = raw.get("x_topics") or {}
        return {**DEFAULTS, **user}
    except Exception:
        return dict(DEFAULTS)


# ============================================================
# 링크 검증 유틸
# ============================================================

def _post_id(url) -> str | None:
    """X URL에서 게시물 ID(숫자)만 추출."""
    if not url:
        return None
    m = re.search(r"/status/(\d+)", str(url))
    return m.group(1) if m else None


def _handle_of(url) -> str | None:
    """X URL에서 계정명 추출. 익명 형식(x.com/i/...)이면 None."""
    if not url:
        return None
    m = re.search(r"(?:x|twitter)\.com/([^/]+)/status/", str(url))
    if not m:
        return None
    h = m.group(1)
    return None if h.lower() == "i" else h


def _verify_links(topics: list[dict], citations: list) -> list[dict]:
    """
    모델이 내놓은 링크를 실제 검색 출처(citations)와 대조.

    출처는 'x.com/i/status/숫자', 모델 답변은 'x.com/계정명/status/숫자' 형식이라
    앞부분은 다르고 뒤의 게시물 ID만 같다. 따라서 ID 기준으로만 비교한다.

    출처에 없는 링크는 모델이 지어낸 것이므로 비우고 verified=False로 표시.
    토픽 내용 자체는 남긴다.
    """
    cite_ids = {_post_id(c) for c in citations}
    cite_ids.discard(None)

    for t in topics:
        url = t.get("sample_post_url")
        if not url:
            t["link_verified"] = False
            t["sample_post_url"] = ""
        elif not cite_ids:
            # 대조할 출처가 없음 — 판단 보류, 링크는 유지하되 표시
            t["link_verified"] = None
        elif _post_id(url) in cite_ids:
            t["link_verified"] = True
        else:
            t["link_verified"] = False
            t["sample_post_url"] = ""

    return topics


# ============================================================
# 프롬프트
# ============================================================

def _build_prompt(hours: int, want: int, exclude_titles: list[str] | None = None) -> str:
    extra = ""
    if exclude_titles:
        joined = " / ".join(exclude_titles)
        extra = f"\n[제외] 아래 주제는 이미 수집했으니 다른 주제를 찾아라: {joined}\n"

    return f"""너는 한국 개인투자자를 위한 시황 브리핑 편집자다.

X에서 최근 {hours}시간 동안 미국 주식·ETF·거시경제와 관련해
반응이 활발했던 주제 {want}개를 찾아라.
{extra}
[검색 방법]
아래 각도를 각각 별도로 검색하라. 최소 5회 이상 검색할 것.
1. 반도체·AI 인프라
2. 빅테크 개별 종목 이슈 (실적, 제품, 규제)
3. 연준·금리·인플레이션 지표
4. 국채 수익률·달러·환율
5. 에너지·원자재·금
6. 당일 특별히 화제가 된 개별 종목

[선별 기준]
- 좋아요·리포스트·인용이 많이 붙은 게시물을 우선한다.
- 단발성 개인 의견보다, 여러 계정이 함께 다룬 사안을 우선한다.

[작성 규칙]
- 실제로 검색된 게시물에만 근거하라. 링크를 절대 지어내지 마라.
- sample_post_url은 검색 결과에 실제로 있던 URL을 그대로 옮겨라. ID를 기억해서 재구성하지 마라.
- 근거 게시물을 못 찾은 토픽은 아예 제외하라.
- sample_post_url은 토픽마다 서로 달라야 한다.
- 개별 종목 추천이나 매수/매도 의견은 절대 쓰지 마라. 사실 전달만 한다.
- '상승 또는 하락', '변동성 확대' 같은 무의미한 문장 금지. 구체적 수치나 사건을 써라.
- 밈, 정치, 스팸은 제외한다.
- {want}개가 안 되면 억지로 채우지 말고 찾은 만큼만 반환하라.

아래 JSON만 출력하라. 코드블록이나 설명 없이.

{{
  "topics": [
    {{
      "rank": 1,
      "title": "주제 한 줄 (한국어, 25자 이내)",
      "tickers": ["티커 배열, 없으면 빈 배열"],
      "summary": "2문장 이내 한국어 사실 서술. 구체적 수치 포함",
      "why_trending": "X에서 왜 화제인지 한 문장",
      "buzz_level": "high | medium | low",
      "sample_post_url": "대표 게시물 URL"
    }}
  ]
}}"""


def _parse_json(text: str) -> dict:
    raw = (text or "").strip()
    if raw.startswith("```"):
        parts = raw.split("```")
        if len(parts) > 1:
            raw = parts[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


# ============================================================
# 수집
# ============================================================

def fetch_x_topics() -> list[dict]:
    """
    xAI X Search로 오늘의 화제 토픽을 수집해서 리스트로 반환.

    1차 수집(기본 12시간)에서 min_topics에 못 미치면
    fallback_hours(기본 24시간)로 한 번 더 훑어 부족분을 채운다.

    실패 시(키 없음 / 패키지 없음 / API 오류) 빈 리스트를 반환한다.
    """
    cfg = _load_x_config()
    if not cfg.get("enabled", True):
        print("  ℹ️  x_topics.enabled=false — X 토픽 수집 건너뜀")
        return []

    api_key = os.getenv("XAI_API_KEY", "")
    if not api_key:
        print("  ⚠️  XAI_API_KEY 미설정 — X 토픽 수집 건너뜀")
        return []

    try:
        from xai_sdk import Client
        from xai_sdk.chat import user
        from xai_sdk.tools import x_search
    except ImportError:
        print("  ⚠️  xai-sdk 패키지 없음 (pip install xai-sdk) — X 토픽 수집 건너뜀")
        return []

    model          = cfg["model"]
    topic_count    = int(cfg["topic_count"])
    lookback_hours = int(cfg["lookback_hours"])
    min_topics     = int(cfg["min_topics"])
    fallback_hours = int(cfg["fallback_hours"])
    handles        = list(cfg.get("handles") or [])[:20]
    use_filter     = bool(cfg.get("use_handle_filter", True)) and bool(handles)

    client = Client(api_key=api_key)
    now = datetime.datetime.now(datetime.timezone.utc)

    def _collect(hours, want, exclude=None):
        kwargs = {
            "from_date": now - datetime.timedelta(hours=hours),
            "to_date": now,
        }
        if use_filter:
            kwargs["allowed_x_handles"] = handles

        chat = client.chat.create(model=model, tools=[x_search(**kwargs)])
        chat.append(user(_build_prompt(hours, want, exclude)))
        resp = chat.sample()

        parsed = _parse_json(resp.content)
        topics = parsed.get("topics") or []
        for t in topics:
            t["window"] = f"{hours}h"
        cites = list(getattr(resp, "citations", []) or [])
        return topics, cites

    try:
        topics, citations = _collect(lookback_hours, topic_count)
        print(f"  → 1차 {lookback_hours}시간: {len(topics)}개 수집")

        if len(topics) < min_topics:
            need = max(topic_count - len(topics), 1)
            seen_titles = [t.get("title", "") for t in topics]
            more, more_cites = _collect(fallback_hours, need, seen_titles)

            have_ids = {_post_id(t.get("sample_post_url")) for t in topics}
            have_ids.discard(None)
            added = [t for t in more if _post_id(t.get("sample_post_url")) not in have_ids]

            topics += added
            citations += more_cites
            print(f"  → {fallback_hours}시간 확대: {len(added)}개 추가 (총 {len(topics)}개)")

    except Exception as e:
        print(f"  ⚠️  X 토픽 수집 실패: {e}")
        return []

    topics = _verify_links(topics, citations)

    for i, t in enumerate(topics, 1):
        t["rank"] = i

    removed = sum(1 for t in topics if t.get("link_verified") is False)
    if removed:
        print(f"  ⚠️  검증 실패 링크 {removed}건 제거 (모델이 지어낸 URL)")

    return topics


# ============================================================
# 저장 / 로드
# ============================================================

def save_x_topics(out_dir: Path, date: str, topics: list[dict]) -> Path:
    """output/x_topics_{date}.json으로 저장."""
    out_dir.mkdir(exist_ok=True)
    path = out_dir / f"x_topics_{date}.json"
    path.write_text(json.dumps(topics, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_latest_x_topics(out_dir: Path, date: str) -> list[dict]:
    """
    output/x_topics_{date}.json을 우선 찾고, 없으면 가장 최근 파일로 대체.
    단 date 기준 1일 이내 파일만 인정 (오래된 데이터가 오늘 브리핑에 섞이지 않도록).
    """
    exact = out_dir / f"x_topics_{date}.json"
    if exact.exists():
        try:
            return json.loads(exact.read_text(encoding="utf-8"))
        except Exception:
            pass

    try:
        target = datetime.datetime.strptime(date, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return []

    candidates = sorted(out_dir.glob("x_topics_*.json"), reverse=True)
    if not candidates:
        return []

    m = re.match(r"x_topics_(\d{4}-\d{2}-\d{2})\.json$", candidates[0].name)
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


# ============================================================
# HTML 렌더링
# ============================================================

def _escape(s) -> str:
    """HTML 특수문자 이스케이프."""
    return (
        str(s or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def render_x_topics_section(topics: list[dict]) -> str:
    """
    X 화제 토픽 섹션 HTML 반환. 빈 리스트면 빈 문자열(섹션 생략).
    기존 hot-topics CSS 클래스를 재사용하므로 별도 스타일 추가가 필요 없다.
    """
    if not topics:
        return ""

    cfg = _load_x_config()
    hours = cfg.get("lookback_hours", 12)

    buzz_color = {"high": "#c2185b", "medium": "#7b1fa2", "low": "#546e7a"}

    cards_html = ""
    for t in topics:
        buzz = str(t.get("buzz_level", "medium")).lower()
        color = buzz_color.get(buzz, "#546e7a")
        tickers = t.get("tickers") or []
        ticker_html = ""
        if tickers:
            ticker_html = (
                "<span style='color:#1a56db;font-weight:700;font-size:12px;margin-left:6px'>"
                + _escape(", ".join(str(x) for x in tickers))
                + "</span>"
            )

        url = t.get("sample_post_url") or ""
        src_btn = (
            f'<a class="hot-topic-src-btn" href="{_escape(url)}" target="_blank" '
            f'rel="noopener">𝕏 원문</a>'
            if url else
            '<span style="color:#b0b0b0;font-size:11px;margin-left:8px">(링크 미검증)</span>'
        )

        window_badge = ""
        if t.get("window") and t["window"] != f"{hours}h":
            window_badge = (
                "<span style='color:#999;font-size:11px;margin-left:6px'>"
                f"{_escape(t['window'])} 구간</span>"
            )

        cards_html += f"""
        <div class="hot-topic-card">
          <span class="hot-topic-rank">#{_escape(t.get('rank', '-'))}</span>
          <span class="hot-topic-format" style="background:{color}">{_escape(buzz)}</span>
          <div class="hot-topic-title">{_escape(t.get('title', ''))}{ticker_html}{src_btn}</div>
          <div class="hot-topic-reason">{_escape(t.get('summary', ''))}</div>
          <div class="hot-topic-reason" style="color:#999">화제 이유: {_escape(t.get('why_trending', ''))}{window_badge}</div>
        </div>"""

    return f"""
    <section class="hot-topics-section">
      <h2>𝕏 에서 화제인 토픽</h2>
      <p class="hot-topics-sub">
        최근 {hours}시간 · 지정 계정 {len(cfg.get('handles') or [])}곳 기준 · Powered by xAI Grok<br>
        언급량을 집계한 수치가 아니라 검색 결과를 바탕으로 한 추정입니다. 투자 판단의 근거가 아닙니다.
      </p>
      <div class="hot-topics-grid">{cards_html}
      </div>
    </section>
    """
