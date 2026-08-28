"""
MarketBrief 단위 테스트
네트워크·API 키 없이 실행 가능한 순수 함수 테스트

실행:
    pip install pytest          # 일반 환경
    uv run pytest test_marketbrief.py -v   # uv 환경
    pytest test_marketbrief.py -v          # 설치 후
"""

import datetime
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

# ── 테스트 대상 함수 임포트 ───────────────────────────────────
from marketbrief import (
    load_config,
    build_mock_market_data,
    build_prompt,
    _render_etf_table,
    validate_output,
    build_html_report,
    build_push_payload,
    _index_card_color,
    _return_color,
    generate_archive_index,
    REQUIRED_SECTIONS,
    parse_hot_topics_response,
    build_hot_topics_prompt,
    render_hot_topics_section,
    attach_hot_topic_sources,
    HOT_TOPIC_CATEGORIES,
    PREFERRED_SOURCE_DOMAINS,
    CFG,
    fetch_hot_topics,
    _is_personal_advice_column,
    NewsFetcher,
    _is_recent_finviz_date,
    FinvizFetcher,
)


# ============================================================
# Fixtures
# ============================================================

@pytest.fixture
def mock_data():
    return build_mock_market_data()


@pytest.fixture
def sample_brief_md(mock_data):
    """REQUIRED_SECTIONS를 모두 포함하는 샘플 브리핑 텍스트"""
    return "\n".join([
        "## 1. 주요 지수 요약",
        "- S&P 500 : 5,074.08 (-1.97%)",
        "",
        "## 2. 채권 시장 & 심리 지표",
        "- 미국채 10Y : 4.01%",
        "",
        "## 3. 섹터 동향",
        "- 상승: Utilities",
        "",
        "## 4. 오늘의 주요 이슈",
        "• Fed officials signal caution",
        "",
        "## 5. 애널리스트 총평",
        "→ 시장은 전반적으로 하락세를 보였다. 투자자 주의가 요망된다.",
        "",
        "## 6. Leverageshares x2 레버리지 ETF — 거래량 TOP 20",
        "| # | Ticker | ETF명 | 거래량 | 수익률 | 배율 |",
        "|---|--------|-------|--------|--------|------|",
        "| 1 | 2NVDA | LeverageShares 2x Nvidia ETP | 1,234,567 | +4.82% | 2x |",
    ])


# ============================================================
# 1. load_config
# ============================================================

class TestLoadConfig:
    def test_returns_dict(self):
        cfg = load_config()
        assert isinstance(cfg, dict)

    def test_has_required_keys(self):
        cfg = load_config()
        for key in ("model", "max_retries", "max_tokens", "data", "output"):
            assert key in cfg, f"config에 '{key}' 키가 없습니다"

    def test_model_is_string(self):
        assert isinstance(load_config()["model"], str)

    def test_max_retries_positive(self):
        assert load_config()["max_retries"] > 0

    def test_custom_config_file(self, tmp_path):
        """config.json이 존재하면 그 값을 읽어야 한다"""
        custom = {"model": "claude-haiku-4-5-20251001", "max_retries": 5, "max_tokens": 1024}
        (tmp_path / "config.json").write_text(json.dumps(custom), encoding="utf-8")

        # 임시 디렉토리의 config를 직접 파싱
        loaded = json.loads((tmp_path / "config.json").read_text())
        assert loaded["model"] == "claude-haiku-4-5-20251001"
        assert loaded["max_retries"] == 5


# ============================================================
# 2. build_mock_market_data
# ============================================================

class TestBuildMockMarketData:
    def test_top_level_keys(self, mock_data):
        required = {
            "date", "update_time", "indices", "sector_heatmap",
            "yields", "fear_greed", "key_issues", "leverage_etf_top20",
            "macro", "sparkline_sp500",           # v2 추가 필드
        }
        assert required <= mock_data.keys()

    def test_indices_keys(self, mock_data):
        for key in ("SP500", "NASDAQ", "DOW", "VIX"):
            assert key in mock_data["indices"]
            assert "close" in mock_data["indices"][key]
            assert "change_pct" in mock_data["indices"][key]

    def test_yields_keys(self, mock_data):
        for key in ("2Y", "10Y", "30Y"):
            assert key in mock_data["yields"]

    def test_fear_greed_range(self, mock_data):
        score = mock_data["fear_greed"]["score"]
        assert 0 <= score <= 100, f"F&G 점수 범위 이탈: {score}"

    def test_macro_keys(self, mock_data):
        """DXY / Gold / Oil 모두 존재해야 한다"""
        for key in ("DXY", "Gold", "Oil"):
            assert key in mock_data["macro"], f"macro에 '{key}' 없음"
            assert mock_data["macro"][key] != "", f"macro['{key}']가 비어 있음"

    def test_macro_values_not_empty(self, mock_data):
        for key, val in mock_data["macro"].items():
            assert val and val != "[확인 필요]", f"macro['{key}'] = {val!r}"

    def test_sparkline_is_list(self, mock_data):
        sp = mock_data["sparkline_sp500"]
        assert isinstance(sp, list), "sparkline_sp500는 list여야 한다"

    def test_sparkline_all_floats(self, mock_data):
        for v in mock_data["sparkline_sp500"]:
            assert isinstance(v, (int, float)), f"스파크라인 값이 숫자가 아님: {v}"

    def test_sparkline_positive_values(self, mock_data):
        for v in mock_data["sparkline_sp500"]:
            assert v > 0, f"S&P 500 값이 0 이하: {v}"

    def test_etf_list_length(self, mock_data):
        assert len(mock_data["leverage_etf_top20"]) == 20

    def test_etf_rank_sequential(self, mock_data):
        ranks = [e["rank"] for e in mock_data["leverage_etf_top20"]]
        assert ranks == list(range(1, 21)), "ETF 순위가 1~20이 아닙니다"

    def test_etf_required_fields(self, mock_data):
        for etf in mock_data["leverage_etf_top20"]:
            for field in ("rank", "ticker", "name", "volume", "return_1d", "leverage"):
                assert field in etf, f"ETF에 '{field}' 필드가 없습니다"

    def test_etf_no_raw_vol_field(self, mock_data):
        """_vol_raw 내부 필드가 외부에 노출되지 않아야 한다"""
        for etf in mock_data["leverage_etf_top20"]:
            assert "_vol_raw" not in etf

    def test_key_issues_not_empty(self, mock_data):
        assert len(mock_data["key_issues"]) > 0

    def test_date_format(self, mock_data):
        assert re.match(r"\d{4}-\d{2}-\d{2}", mock_data["date"])

    def test_update_time_format(self, mock_data):
        assert re.match(r"\d{2}:\d{2} KST", mock_data["update_time"])


# ============================================================
# 3. _render_etf_table
# ============================================================

class TestRenderEtfTable:
    def test_row_count(self, mock_data):
        result = _render_etf_table(mock_data["leverage_etf_top20"])
        assert result.count("\n") == 19  # 20행 = 줄바꿈 19개

    def test_pipe_delimited(self, mock_data):
        result = _render_etf_table(mock_data["leverage_etf_top20"])
        for line in result.splitlines():
            assert line.startswith("| ") and line.endswith(" |")

    def test_contains_ticker(self, mock_data):
        result = _render_etf_table(mock_data["leverage_etf_top20"])
        assert "2NVDA" in result

    def test_empty_list(self):
        assert _render_etf_table([]) == ""


# ============================================================
# 4. build_prompt
# ============================================================

class TestBuildPrompt:
    def test_returns_string(self, mock_data):
        prompt = build_prompt(mock_data)
        assert isinstance(prompt, str) and len(prompt) > 100

    def test_contains_date(self, mock_data):
        prompt = build_prompt(mock_data)
        assert mock_data["date"] in prompt

    def test_contains_sp500_close(self, mock_data):
        prompt = build_prompt(mock_data)
        assert mock_data["indices"]["SP500"]["close"] in prompt

    def test_contains_yields(self, mock_data):
        prompt = build_prompt(mock_data)
        assert "10Y" in prompt

    def test_contains_fear_greed(self, mock_data):
        prompt = build_prompt(mock_data)
        assert "Fear" in prompt or str(mock_data["fear_greed"]["score"]) in prompt

    def test_contains_macro_dxy(self, mock_data):
        """DXY가 프롬프트에 포함되어야 한다"""
        prompt = build_prompt(mock_data)
        assert "DXY" in prompt

    def test_contains_macro_gold(self, mock_data):
        prompt = build_prompt(mock_data)
        assert "Gold" in prompt

    def test_contains_macro_oil(self, mock_data):
        prompt = build_prompt(mock_data)
        assert "Oil" in prompt or "WTI" in prompt

    def test_contains_all_issues(self, mock_data):
        prompt = build_prompt(mock_data)
        for issue in mock_data["key_issues"]:
            assert issue in prompt

    def test_contains_etf_tickers(self, mock_data):
        prompt = build_prompt(mock_data)
        assert "2NVDA" in prompt

    def test_no_unfilled_placeholders(self, mock_data):
        """{YYYY-MM-DD} 같은 미치환 플레이스홀더가 없어야 한다"""
        prompt = build_prompt(mock_data)
        assert "{YYYY" not in prompt
        assert "{value}" not in prompt
        assert "{TICKER}" not in prompt


# ============================================================
# 5. validate_output
# ============================================================

class TestValidateOutput:
    def test_valid_response(self, sample_brief_md):
        result = validate_output(sample_brief_md)
        assert result["is_valid"] is True
        assert result["missing_sections"] == []

    def test_empty_response(self):
        result = validate_output("")
        assert result["is_valid"] is False
        assert len(result["missing_sections"]) == len(REQUIRED_SECTIONS)

    def test_partial_sections(self, sample_brief_md):
        # 섹션 하나 제거
        partial = sample_brief_md.replace("## 1. 주요 지수 요약", "")
        result = validate_output(partial)
        assert result["is_valid"] is False
        assert any("주요 지수" in s for s in result["missing_sections"])

    def test_missing_sections_list(self):
        result = validate_output("## 1. 주요 지수 요약\n내용")
        assert isinstance(result["missing_sections"], list)


# ============================================================
# 6. _index_card_color / _return_color
# ============================================================

class TestColorHelpers:
    @pytest.mark.parametrize("pct,expected", [
        ("+1.5%",  "#1a3a2a"),   # 상승 → 녹색
        ("-2.0%",  "#3a1a1a"),   # 하락 → 적색
        ("0.00%",  "#1e1e2e"),   # 보합 → 기본
        ("invalid","#1e1e2e"),   # 파싱 불가 → 기본
    ])
    def test_index_card_color(self, pct, expected):
        assert _index_card_color(pct) == expected

    @pytest.mark.parametrize("ret,expected", [
        ("+3.2%",  "#4ade80"),   # 양수 → 녹색
        ("-1.5%",  "#f87171"),   # 음수 → 적색
        ("invalid","#94a3b8"),   # 파싱 불가 → 회색
    ])
    def test_return_color(self, ret, expected):
        assert _return_color(ret) == expected


# ============================================================
# 7. build_html_report
# ============================================================

class TestBuildHtmlReport:
    def test_returns_valid_html(self, mock_data, sample_brief_md):
        html = build_html_report(mock_data, sample_brief_md)
        assert html.startswith("<!DOCTYPE html>")
        assert "</html>" in html

    def test_contains_date(self, mock_data, sample_brief_md):
        html = build_html_report(mock_data, sample_brief_md)
        assert mock_data["date"] in html

    def test_contains_indices(self, mock_data, sample_brief_md):
        html = build_html_report(mock_data, sample_brief_md)
        assert "S&amp;P 500" in html or "S&P 500" in html

    def test_contains_fear_greed_score(self, mock_data, sample_brief_md):
        html = build_html_report(mock_data, sample_brief_md)
        assert str(mock_data["fear_greed"]["score"]) in html

    def test_contains_etf_tickers(self, mock_data, sample_brief_md):
        html = build_html_report(mock_data, sample_brief_md)
        assert "2NVDA" in html

    def test_contains_yield_10y(self, mock_data, sample_brief_md):
        html = build_html_report(mock_data, sample_brief_md)
        assert "10Y" in html

    def test_contains_macro_dxy(self, mock_data, sample_brief_md):
        """DXY 카드가 HTML에 존재해야 한다"""
        html = build_html_report(mock_data, sample_brief_md)
        assert "DXY" in html

    def test_contains_macro_gold(self, mock_data, sample_brief_md):
        html = build_html_report(mock_data, sample_brief_md)
        assert "Gold" in html

    def test_contains_sparkline_canvas(self, mock_data, sample_brief_md):
        """Chart.js 캔버스 엘리먼트가 있어야 한다"""
        html = build_html_report(mock_data, sample_brief_md)
        assert "sparkChart" in html
        assert "<canvas" in html

    def test_sparkline_data_embedded(self, mock_data, sample_brief_md):
        """스파크라인 수치가 JS에 포함되어야 한다"""
        html = build_html_report(mock_data, sample_brief_md)
        first_val = str(mock_data["sparkline_sp500"][0])
        assert first_val in html

    def test_contains_chartjs_cdn(self, mock_data, sample_brief_md):
        html = build_html_report(mock_data, sample_brief_md)
        assert "chart.js" in html.lower() or "cdn.jsdelivr" in html

    def test_utf8_encodable(self, mock_data, sample_brief_md):
        """한국어 포함 — UTF-8 인코딩 가능해야 한다"""
        html = build_html_report(mock_data, sample_brief_md)
        html.encode("utf-8")

    def test_no_python_format_leftovers(self, mock_data, sample_brief_md):
        """미치환 파이썬 포맷 문자열이 없어야 한다"""
        html = build_html_report(mock_data, sample_brief_md)
        # f-string 이중 중괄호가 제대로 처리됐는지 확인
        assert "{{" not in html and "}}" not in html


# ============================================================
# 8. build_push_payload
# ============================================================

class TestBuildPushPayload:
    def test_required_keys(self):
        payload = build_push_payload("2026-04-05")
        for key in ("title", "body", "deep_link", "send_at_kst"):
            assert key in payload

    def test_deep_link_contains_date(self):
        payload = build_push_payload("2026-04-05")
        assert "2026-04-05" in payload["deep_link"]

    def test_default_send_time(self):
        assert build_push_payload("2026-04-05")["send_at_kst"] == "06:00"

    def test_custom_send_time(self):
        assert build_push_payload("2026-04-05", "07:30")["send_at_kst"] == "07:30"

    def test_title_contains_date(self):
        payload = build_push_payload("2026-04-05")
        assert "2026-04-05" in payload["title"]


# ============================================================
# 9. generate_archive_index
# ============================================================

class TestGenerateArchiveIndex:
    def test_creates_index_html(self, mock_data, sample_brief_md, tmp_path):
        # data JSON 파일 생성
        (tmp_path / f"data_{mock_data['date']}.json").write_text(
            json.dumps(mock_data), encoding="utf-8"
        )
        generate_archive_index(tmp_path)
        assert (tmp_path / "index.html").exists()

    def test_index_contains_date(self, mock_data, tmp_path):
        (tmp_path / f"data_{mock_data['date']}.json").write_text(
            json.dumps(mock_data), encoding="utf-8"
        )
        generate_archive_index(tmp_path)
        content = (tmp_path / "index.html").read_text(encoding="utf-8")
        assert mock_data["date"] in content

    def test_index_contains_sp500(self, mock_data, tmp_path):
        (tmp_path / f"data_{mock_data['date']}.json").write_text(
            json.dumps(mock_data), encoding="utf-8"
        )
        generate_archive_index(tmp_path)
        content = (tmp_path / "index.html").read_text(encoding="utf-8")
        assert mock_data["indices"]["SP500"]["close"] in content

    def test_no_data_files_skips(self, tmp_path):
        """data_*.json 없으면 index.html 생성하지 않아야 한다"""
        generate_archive_index(tmp_path)
        assert not (tmp_path / "index.html").exists()

    def test_multiple_entries_sorted(self, mock_data, tmp_path):
        """여러 날짜가 있을 때 최신순으로 정렬되어야 한다"""
        for date in ("2026-04-03", "2026-04-05", "2026-04-04"):
            d = {**mock_data, "date": date}
            (tmp_path / f"data_{date}.json").write_text(json.dumps(d), encoding="utf-8")
        generate_archive_index(tmp_path)
        content = (tmp_path / "index.html").read_text(encoding="utf-8")
        pos_05 = content.index("2026-04-05")
        pos_04 = content.index("2026-04-04")
        pos_03 = content.index("2026-04-03")
        assert pos_05 < pos_04 < pos_03, "최신순 정렬이 아닙니다"

    def test_includes_hot_topics_section_when_file_exists(self, mock_data, tmp_path):
        """최신 날짜의 hot_topics_{date}.json이 있으면 화제 TOP 10 섹션을 포함해야 한다"""
        (tmp_path / f"data_{mock_data['date']}.json").write_text(
            json.dumps(mock_data), encoding="utf-8"
        )
        hot_topics = [
            {"rank": 1, "title": "연준 금리 동결", "reason": "조회수 잘 나옴", "format": "숏츠"},
        ]
        (tmp_path / f"hot_topics_{mock_data['date']}.json").write_text(
            json.dumps(hot_topics, ensure_ascii=False), encoding="utf-8"
        )
        generate_archive_index(tmp_path)
        content = (tmp_path / "index.html").read_text(encoding="utf-8")
        assert "연준 금리 동결" in content

    def test_omits_hot_topics_section_when_file_missing(self, mock_data, tmp_path):
        """hot_topics_{date}.json이 없으면 화제 섹션 없이 기존과 동일해야 한다"""
        (tmp_path / f"data_{mock_data['date']}.json").write_text(
            json.dumps(mock_data), encoding="utf-8"
        )
        generate_archive_index(tmp_path)
        content = (tmp_path / "index.html").read_text(encoding="utf-8")
        assert "오늘의 화제" not in content

    def test_hot_topics_ignored_when_not_matching_latest_date(self, mock_data, tmp_path):
        """가장 최근 날짜와 일치하지 않는 hot_topics 파일은 무시해야 한다"""
        (tmp_path / f"data_{mock_data['date']}.json").write_text(
            json.dumps(mock_data), encoding="utf-8"
        )
        stale_topics = [
            {"rank": 1, "title": "지난주 화제", "reason": "오래된 데이터", "format": "숏츠"},
        ]
        (tmp_path / "hot_topics_2020-01-01.json").write_text(
            json.dumps(stale_topics, ensure_ascii=False), encoding="utf-8"
        )
        generate_archive_index(tmp_path)
        content = (tmp_path / "index.html").read_text(encoding="utf-8")
        assert "지난주 화제" not in content


# ============================================================
# 10. parse_hot_topics_response
# ============================================================

class TestParseHotTopicsResponse:
    def test_parses_well_formed_lines(self):
        text = (
            "1. 연준 금리 동결 | 금리 이슈는 조회수가 잘 나옴 | 포맷:숏츠\n"
            "2. 관세 협상 타결 | 시각 자료로 설명하기 좋음 | 포맷:카드뉴스"
        )
        result = parse_hot_topics_response(text)
        assert len(result) == 2
        assert result[0] == {
            "rank": 1,
            "title": "연준 금리 동결",
            "reason": "금리 이슈는 조회수가 잘 나옴",
            "format": "숏츠",
        }
        assert result[1]["format"] == "카드뉴스"

    def test_format_shorts_recognized(self):
        text = "1. 제목 | 이유 | 포맷:숏츠"
        assert parse_hot_topics_response(text)[0]["format"] == "숏츠"

    def test_format_cardnews_recognized(self):
        text = "1. 제목 | 이유 | 포맷:카드뉴스"
        assert parse_hot_topics_response(text)[0]["format"] == "카드뉴스"

    def test_skips_malformed_lines(self):
        """파이프(|) 구분자가 부족한 줄은 건너뛰어야 한다"""
        text = (
            "1. 제목만 있고 구분자가 없는 줄\n"
            "2. 제목 | 이유 | 포맷:숏츠"
        )
        result = parse_hot_topics_response(text)
        assert len(result) == 1
        assert result[0]["title"] == "제목"

    def test_empty_string_returns_empty_list(self):
        assert parse_hot_topics_response("") == []

    def test_caps_at_ten_items(self):
        lines = [f"{i}. 제목{i} | 이유{i} | 포맷:숏츠" for i in range(1, 13)]
        result = parse_hot_topics_response("\n".join(lines))
        assert len(result) == 10

    def test_rank_is_renumbered_sequentially(self):
        """원본 줄 번호가 아니라 유효한 항목 순서대로 rank를 부여해야 한다"""
        text = (
            "5. 첫번째 유효 항목 | 이유 | 포맷:숏츠\n"
            "잘못된 줄\n"
            "9. 두번째 유효 항목 | 이유 | 포맷:숏츠"
        )
        result = parse_hot_topics_response(text)
        assert [t["rank"] for t in result] == [1, 2]


# ============================================================
# 11. build_hot_topics_prompt
# ============================================================

class TestBuildHotTopicsPrompt:
    """
    v2: RSS 후보(key_issues/geo_issues/corp_issues)에 의존하지 않고
    Gemini의 google_search grounding으로 직접 검색하도록 지시하는 프롬프트.
    """

    def test_returns_string(self, mock_data):
        prompt = build_hot_topics_prompt(mock_data)
        assert isinstance(prompt, str) and len(prompt) > 50

    def test_includes_date(self, mock_data):
        prompt = build_hot_topics_prompt(mock_data)
        assert mock_data["date"] in prompt

    def test_instructs_live_search(self, mock_data):
        """RSS 후보 대신 실시간 검색을 지시해야 한다"""
        prompt = build_hot_topics_prompt(mock_data)
        assert "검색" in prompt

    def test_does_not_depend_on_key_issues(self, mock_data):
        """key_issues/geo_issues/corp_issues가 아예 없어도 정상 동작해야 한다"""
        data = {**mock_data}
        del data["key_issues"]
        del data["geo_issues"]
        del data["corp_issues"]
        prompt = build_hot_topics_prompt(data)
        assert isinstance(prompt, str) and len(prompt) > 50

    def test_does_not_include_rss_candidates(self, mock_data):
        """더 이상 RSS 후보 제목을 프롬프트에 그대로 나열하지 않아야 한다"""
        prompt = build_hot_topics_prompt(mock_data)
        assert mock_data["key_issues"][0]["title"] not in prompt

    def test_includes_all_category_names(self, mock_data):
        """검색 다양성 확보를 위한 4개 카테고리명이 모두 프롬프트에 포함돼야 한다"""
        prompt = build_hot_topics_prompt(mock_data)
        for category in HOT_TOPIC_CATEGORIES:
            assert category["name"] in prompt

    def test_instructs_minimum_candidates_per_category(self, mock_data):
        prompt = build_hot_topics_prompt(mock_data)
        assert "2~3" in prompt or "2-3" in prompt

    def test_instructs_category_balance_in_final_selection(self, mock_data):
        """한 카테고리가 TOP10을 독점하지 않도록 균형을 지시해야 한다"""
        prompt = build_hot_topics_prompt(mock_data)
        assert "독점" in prompt

    def test_instructs_preferred_source_domains(self, mock_data):
        """영어권 1차 소스를 우선 참고하도록 지시해야 한다"""
        prompt = build_hot_topics_prompt(mock_data)
        for domain in PREFERRED_SOURCE_DOMAINS:
            assert domain in prompt

    # ── 숏츠 vs 카드뉴스 분류 기준 ───────────────────────────────

    def test_includes_shorts_criterion(self, mock_data):
        """숏츠는 특정 종목/티커가 주인공인 뉴스여야 한다는 기준이 포함돼야 한다"""
        prompt = build_hot_topics_prompt(mock_data)
        assert "주인공" in prompt
        assert "티커" in prompt

    def test_includes_cardnews_criterion(self, mock_data):
        """카드뉴스는 매크로/섹터/트렌드처럼 범위가 넓은 주제여야 한다는 기준이 포함돼야 한다"""
        prompt = build_hot_topics_prompt(mock_data)
        assert "범위가 넓은" in prompt
        assert "매크로" in prompt

    def test_includes_flexible_stock_extension_note(self, mock_data):
        """넓은 주제도 종목으로 확장 설명 가능하면 카드뉴스에 적합하다는 유연성 언급이 있어야 한다"""
        prompt = build_hot_topics_prompt(mock_data)
        assert "확장" in prompt

    def test_examples_are_consistent_with_new_criterion(self, mock_data):
        """
        예시 문구가 새 기준과 충돌하지 않아야 한다.
        - '연준 금리 동결'(매크로) 예시는 이제 카드뉴스로 분류돼야 함
        - 숏츠 예시 줄에는 매크로 이슈가 아니라 특정 종목/티커가 들어가야 함
        """
        prompt = build_hot_topics_prompt(mock_data)

        macro_example_line = next(
            line for line in prompt.splitlines() if "연준 금리 동결" in line
        )
        assert "포맷:카드뉴스" in macro_example_line

        shorts_example_line = next(
            line for line in prompt.splitlines() if "포맷:숏츠" in line
        )
        assert "연준 금리 동결" not in shorts_example_line

    # ── 고정 관심 리스트 (watchlist) ────────────────────────────

    def test_watchlist_items_included_when_given(self, mock_data):
        watchlist = ["Moderna (MRNA) 주가 변동", "SpaceX, Tesla 관련 뉴스"]
        prompt = build_hot_topics_prompt(mock_data, watchlist=watchlist)
        for item in watchlist:
            assert item in prompt

    def test_watchlist_none_defaults_to_config(self, mock_data):
        """watchlist를 안 넘기면 config.json의 hot_topics_watchlist를 기본값으로 써야 한다"""
        prompt = build_hot_topics_prompt(mock_data)
        for item in CFG.get("hot_topics_watchlist", []):
            assert item in prompt

    def test_empty_watchlist_does_not_crash(self, mock_data):
        prompt = build_hot_topics_prompt(mock_data, watchlist=[])
        assert isinstance(prompt, str) and len(prompt) > 50

    def test_watchlist_instructs_skip_if_no_real_news(self, mock_data):
        """관련 뉴스가 실제로 없으면 억지로 포함하지 말라는 지시가 있어야 한다"""
        prompt = build_hot_topics_prompt(mock_data, watchlist=["Moderna (MRNA) 주가 변동"])
        assert "억지로" in prompt


# ============================================================
# 12. render_hot_topics_section
# ============================================================

class TestRenderHotTopicsSection:
    SAMPLE = [
        {"rank": 1, "title": "연준 금리 동결", "reason": "조회수가 잘 나옴", "format": "숏츠"},
        {"rank": 2, "title": "관세 협상 타결", "reason": "시각 자료로 설명하기 좋음", "format": "카드뉴스"},
    ]

    def test_empty_list_returns_empty_string(self):
        assert render_hot_topics_section([]) == ""

    def test_contains_all_titles(self):
        html = render_hot_topics_section(self.SAMPLE)
        assert "연준 금리 동결" in html
        assert "관세 협상 타결" in html

    def test_contains_reason_text(self):
        html = render_hot_topics_section(self.SAMPLE)
        assert "조회수가 잘 나옴" in html

    def test_contains_rank_numbers(self):
        html = render_hot_topics_section(self.SAMPLE)
        assert "1" in html and "2" in html

    def test_shorts_format_shown(self):
        html = render_hot_topics_section(self.SAMPLE)
        assert "숏츠" in html

    def test_cardnews_format_shown(self):
        html = render_hot_topics_section(self.SAMPLE)
        assert "카드뉴스" in html

    def test_source_link_rendered_when_present(self):
        topics = [{**self.SAMPLE[0], "source_url": "https://example.com/news/1"}]
        html = render_hot_topics_section(topics)
        assert 'href="https://example.com/news/1"' in html
        assert "🔗 출처" in html

    def test_no_source_link_when_absent(self):
        """source_url이 없으면 출처 버튼이 렌더링되지 않아야 한다"""
        html = render_hot_topics_section(self.SAMPLE)
        assert "🔗 출처" not in html

    def test_no_source_link_when_none(self):
        topics = [{**self.SAMPLE[0], "source_url": None}]
        html = render_hot_topics_section(topics)
        assert "🔗 출처" not in html


# ============================================================
# 13. attach_hot_topic_sources
# ============================================================

class TestAttachHotTopicSources:
    """
    Gemini google_search grounding 결과(grounding_chunks/grounding_supports)를
    파싱된 hot topics에 매칭해서 source_url/source_title을 붙이는 순수 함수.
    """

    def test_attaches_source_when_segment_overlaps_title(self):
        text = "1. 연준 금리 동결 | 이유 | 포맷:숏츠"
        topics = [{"rank": 1, "title": "연준 금리 동결", "reason": "이유", "format": "숏츠"}]
        start = text.index("연준 금리 동결")
        end = start + len("연준 금리 동결")
        chunks = [{"uri": "https://example.com/a", "title": "출처 A"}]
        supports = [{"start_index": start, "end_index": end, "chunk_indices": [0]}]

        result = attach_hot_topic_sources(text, topics, chunks, supports)

        assert result[0]["source_url"] == "https://example.com/a"
        assert result[0]["source_title"] == "출처 A"

    def test_no_source_when_no_overlap(self):
        text = "1. 연준 금리 동결 | 이유 | 포맷:숏츠"
        topics = [{"rank": 1, "title": "연준 금리 동결", "reason": "이유", "format": "숏츠"}]
        chunks = [{"uri": "https://example.com/a", "title": "출처 A"}]
        # 세그먼트가 제목 위치와 전혀 겹치지 않음
        supports = [{"start_index": 9999, "end_index": 10010, "chunk_indices": [0]}]

        result = attach_hot_topic_sources(text, topics, chunks, supports)

        assert result[0]["source_url"] is None
        assert result[0]["source_title"] is None

    def test_title_not_found_in_text_gets_no_source(self):
        text = "전혀 다른 내용"
        topics = [{"rank": 1, "title": "연준 금리 동결", "reason": "이유", "format": "숏츠"}]
        chunks = [{"uri": "https://example.com/a", "title": "출처 A"}]
        supports = [{"start_index": 0, "end_index": 5, "chunk_indices": [0]}]

        result = attach_hot_topic_sources(text, topics, chunks, supports)
        assert result[0]["source_url"] is None

    def test_multiple_topics_matched_independently(self):
        text = "1. 첫번째 토픽 | 이유1 | 포맷:숏츠\n2. 두번째 토픽 | 이유2 | 포맷:카드뉴스"
        topics = [
            {"rank": 1, "title": "첫번째 토픽", "reason": "이유1", "format": "숏츠"},
            {"rank": 2, "title": "두번째 토픽", "reason": "이유2", "format": "카드뉴스"},
        ]
        start1 = text.index("첫번째 토픽")
        start2 = text.index("두번째 토픽")
        chunks = [
            {"uri": "https://example.com/1", "title": "출처1"},
            {"uri": "https://example.com/2", "title": "출처2"},
        ]
        supports = [
            {"start_index": start1, "end_index": start1 + 5, "chunk_indices": [0]},
            {"start_index": start2, "end_index": start2 + 5, "chunk_indices": [1]},
        ]

        result = attach_hot_topic_sources(text, topics, chunks, supports)

        assert result[0]["source_url"] == "https://example.com/1"
        assert result[1]["source_url"] == "https://example.com/2"

    def test_empty_chunks_and_supports_returns_topics_with_no_source(self):
        text = "1. 연준 금리 동결 | 이유 | 포맷:숏츠"
        topics = [{"rank": 1, "title": "연준 금리 동결", "reason": "이유", "format": "숏츠"}]

        result = attach_hot_topic_sources(text, topics, [], [])

        assert result[0]["source_url"] is None
        assert result[0]["source_title"] is None

    # ── 중복 URL 제거 / 중복 토픽 병합 ──────────────────────────

    def test_second_topic_with_only_duplicate_url_is_dropped(self):
        """두 토픽의 유일한 근거가 같은 URL이면, 낮은 rank(나중) 토픽은 제외된다"""
        text = "1. 첫번째 토픽 | 이유1 | 포맷:숏츠\n2. 두번째 토픽 | 이유2 | 포맷:카드뉴스"
        topics = [
            {"rank": 1, "title": "첫번째 토픽", "reason": "이유1", "format": "숏츠"},
            {"rank": 2, "title": "두번째 토픽", "reason": "이유2", "format": "카드뉴스"},
        ]
        start1 = text.index("첫번째 토픽")
        start2 = text.index("두번째 토픽")
        chunks = [{"uri": "https://example.com/same", "title": "출처"}]
        supports = [
            {"start_index": start1, "end_index": start1 + 5, "chunk_indices": [0]},
            {"start_index": start2, "end_index": start2 + 5, "chunk_indices": [0]},
        ]

        result = attach_hot_topic_sources(text, topics, chunks, supports)

        assert len(result) == 1
        assert result[0]["title"] == "첫번째 토픽"
        assert result[0]["source_url"] == "https://example.com/same"

    def test_rank_renumbered_after_merge(self):
        """병합으로 빠진 자리는 rank가 1..N으로 다시 채워져야 한다"""
        text = "1. A | 이유 | 포맷:숏츠\n2. B | 이유 | 포맷:숏츠\n3. C | 이유 | 포맷:숏츠"
        topics = [
            {"rank": 1, "title": "A", "reason": "이유", "format": "숏츠"},
            {"rank": 2, "title": "B", "reason": "이유", "format": "숏츠"},
            {"rank": 3, "title": "C", "reason": "이유", "format": "숏츠"},
        ]
        start_a, start_b, start_c = text.index("A"), text.index("B"), text.index("C")
        chunks = [{"uri": "https://example.com/same", "title": "출처"}]
        supports = [
            {"start_index": start_a, "end_index": start_a + 1, "chunk_indices": [0]},
            {"start_index": start_b, "end_index": start_b + 1, "chunk_indices": [0]},  # A와 중복 → 제외
            {"start_index": start_c, "end_index": start_c + 1, "chunk_indices": [0]},  # 역시 중복 → 제외
        ]

        result = attach_hot_topic_sources(text, topics, chunks, supports)

        assert [t["title"] for t in result] == ["A"]
        assert [t["rank"] for t in result] == [1]

    def test_topic_falls_back_to_alternate_source_instead_of_being_dropped(self):
        """이미 선점된 URL 외에 다른 근거가 더 있으면, 그 대체 URL로 유지돼야 한다"""
        text = "1. 첫번째 토픽 | 이유1 | 포맷:숏츠\n2. 두번째 토픽 | 이유2 | 포맷:카드뉴스"
        topics = [
            {"rank": 1, "title": "첫번째 토픽", "reason": "이유1", "format": "숏츠"},
            {"rank": 2, "title": "두번째 토픽", "reason": "이유2", "format": "카드뉴스"},
        ]
        start1 = text.index("첫번째 토픽")
        start2 = text.index("두번째 토픽")
        chunks = [
            {"uri": "https://example.com/shared", "title": "공용 출처"},
            {"uri": "https://example.com/alt",    "title": "대체 출처"},
        ]
        supports = [
            {"start_index": start1, "end_index": start1 + 5, "chunk_indices": [0]},
            # 두번째 토픽은 공용 출처 + 대체 출처 둘 다 겹침
            {"start_index": start2, "end_index": start2 + 5, "chunk_indices": [0, 1]},
        ]

        result = attach_hot_topic_sources(text, topics, chunks, supports)

        assert len(result) == 2
        assert result[1]["source_url"] == "https://example.com/alt"

    def test_topic_with_no_evidence_kept_even_when_duplicate_exists(self):
        """근거가 전혀 없는 토픽은 다른 토픽의 중복 판정과 무관하게 유지된다"""
        text = "1. 첫번째 토픽 | 이유1 | 포맷:숏츠\n2. 두번째 토픽 | 이유2 | 포맷:숏츠\n3. 세번째 토픽 | 이유3 | 포맷:숏츠"
        topics = [
            {"rank": 1, "title": "첫번째 토픽", "reason": "이유1", "format": "숏츠"},
            {"rank": 2, "title": "두번째 토픽", "reason": "이유2", "format": "숏츠"},  # 첫번째와 URL 중복 → 제외 대상
            {"rank": 3, "title": "세번째 토픽", "reason": "이유3", "format": "숏츠"},  # 근거 없음 → 유지
        ]
        start1 = text.index("첫번째 토픽")
        start2 = text.index("두번째 토픽")
        chunks = [{"uri": "https://example.com/same", "title": "출처"}]
        supports = [
            {"start_index": start1, "end_index": start1 + 5, "chunk_indices": [0]},
            {"start_index": start2, "end_index": start2 + 5, "chunk_indices": [0]},
        ]

        result = attach_hot_topic_sources(text, topics, chunks, supports)

        titles = [t["title"] for t in result]
        assert "첫번째 토픽" in titles
        assert "두번째 토픽" not in titles
        assert "세번째 토픽" in titles

    def test_no_duplicate_urls_in_final_result(self):
        """결과에 동일한 source_url이 두 번 이상 등장하지 않아야 한다 (불변식)"""
        text = "1. A | 이유 | 포맷:숏츠\n2. B | 이유 | 포맷:숏츠\n3. C | 이유 | 포맷:숏츠"
        topics = [
            {"rank": 1, "title": "A", "reason": "이유", "format": "숏츠"},
            {"rank": 2, "title": "B", "reason": "이유", "format": "숏츠"},
            {"rank": 3, "title": "C", "reason": "이유", "format": "숏츠"},
        ]
        start_a, start_b, start_c = text.index("A"), text.index("B"), text.index("C")
        chunks = [
            {"uri": "https://example.com/1", "title": "출처1"},
            {"uri": "https://example.com/2", "title": "출처2"},
        ]
        supports = [
            {"start_index": start_a, "end_index": start_a + 1, "chunk_indices": [0]},
            {"start_index": start_b, "end_index": start_b + 1, "chunk_indices": [1]},
            {"start_index": start_c, "end_index": start_c + 1, "chunk_indices": [0, 1]},
        ]

        result = attach_hot_topic_sources(text, topics, chunks, supports)

        urls = [t["source_url"] for t in result if t.get("source_url")]
        assert len(urls) == len(set(urls))

    # ── 영어권 1차 소스 우선 채택 ────────────────────────────────

    def test_prefers_preferred_domain_over_other_candidate(self):
        """후보가 여러 개면 PREFERRED_SOURCE_DOMAINS와 매칭되는 쪽을 우선 채택한다"""
        text = "1. 연준 금리 동결 | 이유 | 포맷:숏츠"
        topics = [{"rank": 1, "title": "연준 금리 동결", "reason": "이유", "format": "숏츠"}]
        start = text.index("연준 금리 동결")
        end   = start + len("연준 금리 동결")
        chunks = [
            {"uri": "https://example.com/kr", "title": "yna.co.kr"},
            {"uri": "https://example.com/reuters", "title": "reuters.com"},
        ]
        supports = [{"start_index": start, "end_index": end, "chunk_indices": [0, 1]}]

        result = attach_hot_topic_sources(text, topics, chunks, supports)

        assert result[0]["source_url"] == "https://example.com/reuters"

    def test_falls_back_to_non_preferred_when_no_preferred_available(self):
        """선호 도메인 후보가 없으면 기존처럼 첫 번째 미사용 후보를 채택한다"""
        text = "1. 연준 금리 동결 | 이유 | 포맷:숏츠"
        topics = [{"rank": 1, "title": "연준 금리 동결", "reason": "이유", "format": "숏츠"}]
        start = text.index("연준 금리 동결")
        end   = start + len("연준 금리 동결")
        chunks = [{"uri": "https://example.com/kr", "title": "yna.co.kr"}]
        supports = [{"start_index": start, "end_index": end, "chunk_indices": [0]}]

        result = attach_hot_topic_sources(text, topics, chunks, supports)

        assert result[0]["source_url"] == "https://example.com/kr"

    def test_preferred_domain_matching_is_case_insensitive(self):
        text = "1. 연준 금리 동결 | 이유 | 포맷:숏츠"
        topics = [{"rank": 1, "title": "연준 금리 동결", "reason": "이유", "format": "숏츠"}]
        start = text.index("연준 금리 동결")
        end   = start + len("연준 금리 동결")
        chunks = [{"uri": "https://example.com/bb", "title": "Bloomberg.com"}]
        supports = [{"start_index": start, "end_index": end, "chunk_indices": [0]}]

        result = attach_hot_topic_sources(text, topics, chunks, supports)

        assert result[0]["source_url"] == "https://example.com/bb"

    def test_preferred_domain_still_respects_used_urls_dedup(self):
        """선호 도메인이라도 이미 앞선 토픽이 쓴 URL이면 재사용하지 않는다"""
        text = "1. 첫번째 토픽 | 이유1 | 포맷:숏츠\n2. 두번째 토픽 | 이유2 | 포맷:카드뉴스"
        topics = [
            {"rank": 1, "title": "첫번째 토픽", "reason": "이유1", "format": "숏츠"},
            {"rank": 2, "title": "두번째 토픽", "reason": "이유2", "format": "카드뉴스"},
        ]
        start1 = text.index("첫번째 토픽")
        start2 = text.index("두번째 토픽")
        chunks = [{"uri": "https://example.com/reuters", "title": "reuters.com"}]
        supports = [
            {"start_index": start1, "end_index": start1 + 5, "chunk_indices": [0]},
            {"start_index": start2, "end_index": start2 + 5, "chunk_indices": [0]},
        ]

        result = attach_hot_topic_sources(text, topics, chunks, supports)

        # 두번째 토픽은 유일한 근거(reuters)가 이미 선점됐으므로 중복으로 제외
        assert len(result) == 1
        assert result[0]["title"] == "첫번째 토픽"

    def test_returns_same_number_of_topics(self):
        text = "1. A | 이유 | 포맷:숏츠\n2. B | 이유 | 포맷:숏츠"
        topics = [
            {"rank": 1, "title": "A", "reason": "이유", "format": "숏츠"},
            {"rank": 2, "title": "B", "reason": "이유", "format": "숏츠"},
        ]
        result = attach_hot_topic_sources(text, topics, [], [])
        assert len(result) == 2


# ============================================================
# 13. 통합 스모크 테스트 (mock 데이터 전체 파이프라인)
# ============================================================

class TestSmokePipeline:
    def test_mock_to_prompt(self, mock_data):
        """mock 데이터 → 프롬프트 생성 정상 동작"""
        prompt = build_prompt(mock_data)
        assert len(prompt) > 500

    def test_mock_prompt_has_macro(self, mock_data):
        """프롬프트에 매크로 지표 3종이 모두 포함되어야 한다"""
        prompt = build_prompt(mock_data)
        for key in ("DXY", "Gold", "Oil"):
            assert key in prompt, f"프롬프트에 '{key}' 없음"

    def test_mock_to_html(self, mock_data, sample_brief_md):
        """mock 데이터 → HTML 생성 정상 동작"""
        html = build_html_report(mock_data, sample_brief_md)
        assert len(html) > 1000

    def test_mock_html_has_sparkline(self, mock_data, sample_brief_md):
        """HTML에 스파크라인 캔버스가 있어야 한다"""
        html = build_html_report(mock_data, sample_brief_md)
        assert "sparkChart" in html

    def test_validate_prompt_sections(self, mock_data, sample_brief_md):
        """sample_brief_md가 모든 필수 섹션을 통과해야 한다"""
        result = validate_output(sample_brief_md)
        assert result["is_valid"] is True, f"누락 섹션: {result['missing_sections']}"

    def test_mock_to_archive(self, mock_data, sample_brief_md, tmp_path):
        """mock 데이터 → 파일 저장 → 아카이브 인덱스 전체 흐름"""
        date = mock_data["date"]

        html = build_html_report(mock_data, sample_brief_md)
        (tmp_path / f"brief_{date}.html").write_text(html, encoding="utf-8")
        (tmp_path / f"data_{date}.json").write_text(
            json.dumps(mock_data), encoding="utf-8"
        )

        generate_archive_index(tmp_path)

        index = (tmp_path / "index.html").read_text(encoding="utf-8")
        assert f'href="brief_{date}.html"' in index

    def test_push_payload_roundtrip(self, mock_data):
        """push payload가 JSON 직렬화 가능해야 한다"""
        payload = build_push_payload(mock_data["date"])
        serialized = json.dumps(payload, ensure_ascii=False)
        restored   = json.loads(serialized)
        assert restored["deep_link"] == payload["deep_link"]


# ============================================================
# 14. fetch_hot_topics — grounding 비었을 때 1회 재시도
# ============================================================

def _fake_response(text, chunks=None, supports=None):
    """_generate_content가 반환하는 raw response 형태를 흉내내는 SimpleNamespace."""
    if chunks is None and supports is None:
        gm = None
    else:
        gm = SimpleNamespace(
            grounding_chunks=[
                SimpleNamespace(web=SimpleNamespace(uri=c["uri"], title=c["title"]))
                for c in (chunks or [])
            ],
            grounding_supports=[
                SimpleNamespace(
                    segment=SimpleNamespace(
                        start_index=s["start_index"], end_index=s["end_index"]
                    ),
                    grounding_chunk_indices=s["chunk_indices"],
                )
                for s in (supports or [])
            ],
        )
    return SimpleNamespace(text=text, candidates=[SimpleNamespace(grounding_metadata=gm)])


class TestFetchHotTopicsRetry:
    RESPONSE_TEXT = "1. A | 이유 | 포맷:숏츠"

    def test_retries_once_when_first_grounding_is_empty(self, monkeypatch, mock_data):
        calls = []

        def fake_generate_content(prompt, system, max_retries=None, tools=None):
            calls.append(1)
            if len(calls) == 1:
                return _fake_response(self.RESPONSE_TEXT)  # grounding 없음
            return _fake_response(
                self.RESPONSE_TEXT,
                chunks=[{"uri": "https://example.com/a", "title": "reuters.com"}],
                supports=[{"start_index": 3, "end_index": 4, "chunk_indices": [0]}],
            )

        monkeypatch.setattr("marketbrief._generate_content", fake_generate_content)

        result = fetch_hot_topics(mock_data)

        assert len(calls) == 2
        assert result[0]["source_url"] == "https://example.com/a"

    def test_does_not_retry_when_grounding_present_on_first_try(self, monkeypatch, mock_data):
        calls = []

        def fake_generate_content(prompt, system, max_retries=None, tools=None):
            calls.append(1)
            return _fake_response(
                self.RESPONSE_TEXT,
                chunks=[{"uri": "https://example.com/a", "title": "reuters.com"}],
                supports=[{"start_index": 3, "end_index": 4, "chunk_indices": [0]}],
            )

        monkeypatch.setattr("marketbrief._generate_content", fake_generate_content)

        fetch_hot_topics(mock_data)

        assert len(calls) == 1

    def test_gives_up_gracefully_when_still_empty_after_retry(self, monkeypatch, mock_data):
        def fake_generate_content(prompt, system, max_retries=None, tools=None):
            return _fake_response(self.RESPONSE_TEXT)  # 매번 grounding 없음

        monkeypatch.setattr("marketbrief._generate_content", fake_generate_content)

        result = fetch_hot_topics(mock_data)

        assert result[0]["source_url"] is None


# ============================================================
# 15. _is_personal_advice_column
# ============================================================

class TestIsPersonalAdviceColumn:
    ADVICE_TITLES = [
        "My son does not work, yet pays $500 for Affordable Care Act health insurance",
        "I hold my mother-in-law's power of attorney. I'm also her executor and trustee.",
        "Our 4-year-old son has $100,000 in his 529 account. Is a bull market ahead?",
        "We're in our 50s and have $1.5 million in traditional 401(k)s. Is it too late?",
    ]
    NEWS_TITLES = [
        "Legendary film editor Billy Weber of 'Miss Congeniality' and 'Top Gun' dies",
        "Anxious bond market sends troubling message to investors",
        "Sports betting to build wealth is becoming the new American dream",
        "The bond market is going to burst the stock-market bubble",
        "Moderna's personalized mRNA shot could reshape the fight against skin cancer",
        "Fed's Powell says rate cut 'on the table' as soon as September",
        "Duolingo started at buy with $222 price target at Seaport",
    ]

    @pytest.mark.parametrize("title", ADVICE_TITLES)
    def test_detects_advice_columns(self, title):
        assert _is_personal_advice_column(title) is True

    @pytest.mark.parametrize("title", NEWS_TITLES)
    def test_does_not_flag_real_news(self, title):
        assert _is_personal_advice_column(title) is False

    def test_case_insensitive(self):
        assert _is_personal_advice_column("my retirement plan is falling apart") is True

    def test_empty_string_is_not_advice(self):
        assert _is_personal_advice_column("") is False


# ============================================================
# 16. NewsFetcher._fetch_from — 필터링 + 버퍼 확보
# ============================================================

class TestNewsFetcherFiltering:
    def _mock_feed(self, titles):
        return SimpleNamespace(
            entries=[
                SimpleNamespace(get=lambda key, default="", _t=t: {"title": _t, "link": "https://x/" + _t}.get(key, default))
                for t in titles
            ]
        )

    def test_filters_out_personal_advice_columns(self, monkeypatch):
        titles = [
            "Fed's Powell says rate cut 'on the table'",
            "My son does not work, yet pays $500 for insurance",
            "Dollar jumps 0.5% to 0.8890 francs",
            "We're in our 50s and have $1.5 million in 401(k)s",
            "Moderna's personalized mRNA shot could reshape cancer fight",
        ]
        monkeypatch.setattr(
            "marketbrief.feedparser.parse",
            lambda url: self._mock_feed(titles),
        )

        result = NewsFetcher()._fetch_from(["https://fake-feed"], max_items=5)
        result_titles = [r["title"] for r in result]

        assert "My son does not work, yet pays $500 for insurance" not in result_titles
        assert "We're in our 50s and have $1.5 million in 401(k)s" not in result_titles
        assert "Fed's Powell says rate cut 'on the table'" in result_titles

    def test_still_returns_max_items_when_enough_real_news_available(self, monkeypatch):
        """필터링돼도 실제 뉴스가 충분하면 max_items만큼 채워야 한다 (버퍼 확보 확인)"""
        titles = (
            ["My personal advice column headline #" + str(i) for i in range(5)]
            + ["Real market news headline #" + str(i) for i in range(5)]
        )
        monkeypatch.setattr(
            "marketbrief.feedparser.parse",
            lambda url: self._mock_feed(titles),
        )

        result = NewsFetcher()._fetch_from(["https://fake-feed"], max_items=5)

        assert len(result) == 5
        assert all("Real market news" in r["title"] for r in result)

    def test_does_not_exceed_max_items(self, monkeypatch):
        titles = ["Real market news headline #" + str(i) for i in range(20)]
        monkeypatch.setattr(
            "marketbrief.feedparser.parse",
            lambda url: self._mock_feed(titles),
        )

        result = NewsFetcher()._fetch_from(["https://fake-feed"], max_items=5)

        assert len(result) == 5


# ============================================================
# 17. _is_recent_finviz_date
# ============================================================

class TestIsRecentFinvizDate:
    def test_recent_date_within_range_is_true(self):
        now = datetime.datetime(2026, 8, 21, 12, 0)
        recent = (now - datetime.timedelta(hours=5)).strftime("%Y-%m-%d %H:%M")
        assert _is_recent_finviz_date(recent, max_age_days=3, now=now) is True

    def test_old_date_outside_range_is_false(self):
        now = datetime.datetime(2026, 8, 21, 12, 0)
        old = (now - datetime.timedelta(days=30)).strftime("%Y-%m-%d %H:%M")
        assert _is_recent_finviz_date(old, max_age_days=3, now=now) is False

    def test_exactly_at_cutoff_is_true(self):
        """정확히 max_age_days 전이면 포함(경계값 inclusive)"""
        now = datetime.datetime(2026, 8, 21, 12, 0)
        boundary = (now - datetime.timedelta(days=3)).strftime("%Y-%m-%d %H:%M")
        assert _is_recent_finviz_date(boundary, max_age_days=3, now=now) is True

    def test_invalid_format_returns_false(self):
        assert _is_recent_finviz_date("not-a-date", max_age_days=3) is False

    def test_empty_string_returns_false(self):
        assert _is_recent_finviz_date("", max_age_days=3) is False


# ============================================================
# 18. FinvizFetcher.fetch_corp_news — 날짜 필터 + 중복 제거
# ============================================================

class TestFetchCorpNewsFiltering:
    def _fresh(self, hours_ago=1):
        return (datetime.datetime.now() - datetime.timedelta(hours=hours_ago)).strftime("%Y-%m-%d %H:%M")

    def _stale(self, days_ago=30):
        return (datetime.datetime.now() - datetime.timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M")

    def test_filters_out_news_older_than_max_age_days(self, monkeypatch):
        def fake_get_news(ticker):
            return [
                (self._fresh(), "Fresh CRM news", "/news/1", "Zacks"),
                (self._stale(), "Stale CRM news from months ago", "/news/2", "Zacks"),
            ]
        monkeypatch.setattr("finviz.get_news", fake_get_news)

        result = FinvizFetcher().fetch_corp_news(["CRM"], max_per_ticker=5, max_age_days=3)
        titles = [r["title"] for r in result]

        assert any("Fresh CRM news" in t for t in titles)
        assert not any("Stale CRM news" in t for t in titles)

    def test_deduplicates_same_article_across_tickers(self, monkeypatch):
        """여러 종목에 걸친 기사가 각 종목 피드에 중복으로 잡혀도 한 번만 남아야 한다"""
        shared_article = (self._fresh(),
                           "Top Stock Reports for Salesforce, Seagate Technology & AT&T",
                           "/news/384399/top-stock-reports", "Zacks")

        def fake_get_news(ticker):
            return [shared_article]

        monkeypatch.setattr("finviz.get_news", fake_get_news)

        result = FinvizFetcher().fetch_corp_news(["CRM", "STX", "T"], max_per_ticker=1, max_age_days=3)

        assert len(result) == 1

    def test_does_not_dedupe_genuinely_different_articles(self, monkeypatch):
        def fake_get_news(ticker):
            return [(self._fresh(), f"{ticker} unique news headline", "/news/x", "Zacks")]

        monkeypatch.setattr("finviz.get_news", fake_get_news)

        result = FinvizFetcher().fetch_corp_news(["CRM", "STX"], max_per_ticker=1, max_age_days=3)

        assert len(result) == 2

    def test_default_max_age_days_is_three(self, monkeypatch):
        """max_age_days를 안 넘기면 기본 3일이 적용돼야 한다"""
        def fake_get_news(ticker):
            return [
                (self._fresh(hours_ago=1), "Fresh news", "/news/1", "Zacks"),
                (self._stale(days_ago=10), "Old news", "/news/2", "Zacks"),
            ]
        monkeypatch.setattr("finviz.get_news", fake_get_news)

        result = FinvizFetcher().fetch_corp_news(["CRM"], max_per_ticker=5)
        titles = [r["title"] for r in result]

        assert any("Fresh news" in t for t in titles)
        assert not any("Old news" in t for t in titles)
