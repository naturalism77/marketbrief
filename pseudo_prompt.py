# ============================================================
# MarketBrief — Daily US Market Brief Generator
# Pseudo Prompt (Python Style)
# ============================================================

SYSTEM_PERSONA = """
You are a seasoned Wall Street analyst with 20 years of experience.
Your writing style is sharp, concise, and insight-driven.
You deliver market summaries that busy professionals can absorb in under 5 minutes.
Never speculate — if data is uncertain, explicitly mark it as [확인 필요].
"""

# ------------------------------------------------------------
# 1. DATA INPUTS (filled by data pipeline before prompt call)
# ------------------------------------------------------------

market_data = {
    "date": "{YYYY-MM-DD}",                         # e.g. 2026-04-05 (전일 기준)
    "update_time": "{HH:MM KST}",
    "indices": {
        "SP500":  {"close": "{value}", "change_pct": "{+/-X.XX%}"},
        "NASDAQ": {"close": "{value}", "change_pct": "{+/-X.XX%}"},
        "DOW":    {"close": "{value}", "change_pct": "{+/-X.XX%}"},
        "VIX":    {"close": "{value}", "change_pct": "{+/-X.XX%}"},
    },
    "sector_heatmap": {
        "top_gainers":  ["{섹터명 +X.XX%}", "{섹터명 +X.XX%}", "{섹터명 +X.XX%}"],
        "top_losers":   ["{섹터명 -X.XX%}", "{섹터명 -X.XX%}", "{섹터명 -X.XX%}"],
    },
    "key_issues": [
        "{이슈 1: Fed 발언 / 금리 결정 등}",
        "{이슈 2: 주요 경제지표 발표}",
        "{이슈 3: 지정학적 리스크}",
        "{이슈 4: 개별 기업 이슈}",
        "{이슈 5: 기타}",
    ],
    "leverage_etf_top20": [
        # 아래 구조가 20개 반복
        {
            "rank": 1,
            "ticker":   "{TICKER}",
            "name":     "{ETF 정식명}",
            "volume":   "{거래량(주)}",
            "return_1d":"{+/-X.XX%}",
            "leverage": "2x",
        },
        # ... (rank 2 ~ 20)
    ],
}

# ------------------------------------------------------------
# 2. PROMPT TEMPLATE
# ------------------------------------------------------------

def build_prompt(data: dict) -> str:
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

## 2. 섹터 동향
- 상승 상위 섹터: {', '.join(data['sector_heatmap']['top_gainers'])}
- 하락 상위 섹터: {', '.join(data['sector_heatmap']['top_losers'])}

## 3. 오늘의 주요 이슈 (3~5개 불릿)
{chr(10).join(f'• {issue}' for issue in data['key_issues'])}

## 4. 애널리스트 총평
→ [1~2문장. 시장 전체 흐름과 투자자 시사점을 핵심만 담아 작성]
   불확실한 내용은 반드시 [확인 필요] 태그를 붙일 것.

## 5. Leverageshares x2 레버리지 ETF — 거래량 TOP 20
| # | Ticker | ETF명 | 거래량 | 수익률 | 배율 |
|---|--------|-------|--------|--------|------|
{_render_etf_table(data['leverage_etf_top20'])}

[작성 규칙]
- 문체   : 간결·직관적. 문장은 짧게. 군더더기 제거.
- 수치   : 제공된 데이터 그대로 사용. 임의로 추정하지 말 것.
- 불확실 : 확인되지 않은 정보는 [확인 필요] 명시.
- 분량   : 총평 2문장 이내 / 이슈 불릿 5개 이내 / 표는 20행 고정.
- 언어   : 한국어 (수치·ticker는 영문 그대로 유지).
"""
    return prompt.strip()


def _render_etf_table(etf_list: list) -> str:
    """ETF 리스트를 마크다운 테이블 행으로 변환"""
    rows = []
    for etf in etf_list:
        row = (
            f"| {etf['rank']} "
            f"| {etf['ticker']} "
            f"| {etf['name']} "
            f"| {etf['volume']} "
            f"| {etf['return_1d']} "
            f"| {etf['leverage']} |"
        )
        rows.append(row)
    return "\n".join(rows)


# ------------------------------------------------------------
# 3. API CALL (Claude)
# ------------------------------------------------------------

def call_claude(prompt: str, system: str) -> str:
    """
    Anthropic Claude API 호출 (실제 구현 시 anthropic SDK 사용)

    import anthropic
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    message = client.messages.create(
        model   = "claude-opus-4-6",
        max_tokens = 2048,
        system  = system,
        messages = [{"role": "user", "content": prompt}],
    )
    return message.content[0].text
    """
    return "[Claude API 응답이 여기에 반환됩니다]"


# ------------------------------------------------------------
# 4. OUTPUT VALIDATION
# ------------------------------------------------------------

REQUIRED_SECTIONS = [
    "## 1. 주요 지수 요약",
    "## 2. 섹터 동향",
    "## 3. 오늘의 주요 이슈",
    "## 4. 애널리스트 총평",
    "## 5. Leverageshares x2 레버리지 ETF",
]

def validate_output(response: str) -> dict:
    """생성된 브리핑이 필수 섹션을 모두 포함하는지 검증"""
    missing = [s for s in REQUIRED_SECTIONS if s not in response]
    return {
        "is_valid": len(missing) == 0,
        "missing_sections": missing,
    }


# ------------------------------------------------------------
# 5. MAIN PIPELINE
# ------------------------------------------------------------

def generate_daily_brief(raw_data: dict) -> str:
    """
    MarketBrief 일일 시황 생성 메인 파이프라인
    호출 시각: 매일 04:00~05:00 KST (미국 증시 마감 후)
    목표 발행: 06:00 KST 이전 완료
    """
    # Step 1 — 프롬프트 빌드
    prompt = build_prompt(raw_data)

    # Step 2 — Claude API 호출
    response = call_claude(prompt=prompt, system=SYSTEM_PERSONA)

    # Step 3 — 출력 검증
    validation = validate_output(response)
    if not validation["is_valid"]:
        raise ValueError(f"누락된 섹션: {validation['missing_sections']}")

    # Step 4 — 반환 (DB 저장 / 푸시알림 트리거로 전달)
    return response


# ------------------------------------------------------------
# 6. PUSH NOTIFICATION PAYLOAD
# ------------------------------------------------------------

def build_push_payload(brief_date: str) -> dict:
    """FCM / APNs 푸시 알림 페이로드"""
    return {
        "title": f"📊 {brief_date} 미증시 시황 업데이트",
        "body":  "오늘 꼭 알아야 할 미국 증시 핵심, 지금 확인하세요.",
        "deep_link": f"marketbrief://brief/{brief_date}",
        "send_at_kst": "06:00",          # 기본값. 사용자 설정(05:00~09:00) 반영
    }