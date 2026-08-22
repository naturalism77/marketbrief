# 📊 MarketBrief

> **미국 증시 일일 시황 자동 생성 시스템**  
> yfinance로 데이터를 수집하고 Claude API로 한국어 브리핑을 생성합니다.

---

## 목차

1. [개요](#개요)
2. [아키텍처](#아키텍처)
3. [빠른 시작](#빠른-시작)
4. [GitHub Actions 자동화](#github-actions-자동화)
5. [CLI 사용법](#cli-사용법)
6. [설정 파일](#설정-파일-configjson)
7. [환경 변수](#환경-변수-env)
8. [출력 파일](#출력-파일)
9. [알림 설정](#알림-설정)
10. [개발자 가이드](#개발자-가이드)

---

## 개요

**MarketBrief**는 매일 오전 6시(KST) 미국 증시 마감 데이터를 자동 수집해  
출근길 투자자가 5분 안에 핵심을 파악할 수 있는 시황 브리핑을 생성합니다.

### 수집 데이터

| 항목 | 소스 | 내용 |
|------|------|------|
| 주요 지수 | yfinance | S&P 500 / NASDAQ / DOW / VIX 종가·등락률 |
| 국채 금리 | yfinance | 미국채 2Y / 10Y / 30Y |
| Fear & Greed | CNN API | 0~100 점수 + 등급 |
| 섹터 동향 | yfinance | 11개 섹터 ETF 기반 상승·하락 TOP 3 |
| 뉴스 | RSS | MarketWatch / Reuters / CNN Money |
| LS ETF TOP 20 | yfinance (LSE) | LeverageShares 2x 단일주 ETP 거래량 순위 |

### 생성 결과

```
output/
├── brief_YYYY-MM-DD.md        # Claude가 작성한 마크다운 브리핑
├── brief_YYYY-MM-DD.html      # 다크 테마 시각화 리포트
├── data_YYYY-MM-DD.json       # 수집된 raw 시장 데이터
├── push_YYYY-MM-DD.json       # FCM/APNs 푸시 알림 페이로드
└── index.html                 # 전체 브리핑 아카이브 인덱스
```

---

## 아키텍처

```
[Data Layer]                  [AI Layer]               [Output Layer]
─────────────────             ────────────             ──────────────────────
yfinance (지수/금리/ETF)  ──┐                          output/brief_*.md
CNN API  (Fear & Greed)   ──┼──▶ build_prompt() ──▶   output/brief_*.html
RSS Feed (뉴스 헤드라인)   ──┘         │               output/data_*.json
                                       ▼               output/index.html
                               call_claude()
                               (Claude API)            [Notifications]
                                       │               ──────────────────────
                               validate_output()  ──▶  Email (Gmail SMTP)
                                       │               Slack Webhook
                               build_html_report()     Push Payload (FCM/APNs)
```

```
[Automation]
─────────────────────────────────────────────────────────
GitHub Actions  : 매일 21:00 UTC (= 06:00 KST) 자동 실행
Windows Scheduler: schedule_task.bat 으로 로컬 자동화
```

---

## 빠른 시작

### 요구사항

- Python 3.11+
- Anthropic API 키 ([발급](https://console.anthropic.com/))

### 설치

```bash
# 1. 저장소 클론
git clone https://github.com/YOUR_USERNAME/marketbrief.git
cd marketbrief

# 2. 패키지 설치  (Windows: setup.bat 더블클릭)
pip install -r requirements.txt

# 3. 환경 변수 설정
copy .env.example .env
# .env 열어서 ANTHROPIC_API_KEY 입력
```

### 테스트 (API 키 없이)

```bash
# 단위 테스트 실행
pytest test_marketbrief.py -v

# Mock 데이터로 전체 파이프라인 검증 + 브라우저 미리보기
python marketbrief.py --test --open
```

### 실제 실행

```bash
python marketbrief.py
```

---

## GitHub Actions 자동화

**로컬 Python 없이 클라우드에서 자동 실행**됩니다.

### 설정 순서

```bash
# 1. GitHub 저장소 생성 (private 권장)
gh repo create marketbrief --private --source=. --push

# 2. Secrets 등록
gh secret set ANTHROPIC_API_KEY   # Claude API 키 (필수)
gh secret set SLACK_WEBHOOK_URL   # Slack 알림 (선택)
gh secret set EMAIL_SENDER        # Gmail 주소 (선택)
gh secret set EMAIL_PASSWORD      # Gmail 앱 비밀번호 (선택)
gh secret set EMAIL_RECIPIENTS    # 수신자 목록 (선택)
```

### 동작 방식

| 이벤트 | 시각 | 동작 |
|--------|------|------|
| 스케줄 (월~금) | 06:00 KST | 단위 테스트 → 브리핑 생성 → output/ 커밋·푸시 → Slack 알림 |
| 수동 실행 | 언제든 | GitHub Actions 탭 → `workflow_dispatch` |
| 실패 | 즉시 | Slack 에러 알림 |

### GitHub Pages로 아카이브 공개 (선택)

```
Settings → Pages → Source: Deploy from branch
Branch: main / Folder: /output
```

`https://YOUR_USERNAME.github.io/marketbrief/` 에서 브리핑 열람 가능

---

## CLI 사용법

```bash
python marketbrief.py [옵션]
```

| 옵션 | 설명 |
|------|------|
| *(없음)* | 정상 실행: 데이터 수집 → Claude API → 파일 저장 |
| `--test` | Mock 데이터로 전체 파이프라인 검증 (네트워크·API 불필요) |
| `--dry-run` | 데이터 수집 + 프롬프트 출력만, Claude API 미호출 |
| `--show-data` | 수집된 raw JSON 출력 후 종료 |
| `--open` | 완료 후 HTML 리포트를 브라우저로 자동 열기 |
| `--email` | `.env`의 `EMAIL_RECIPIENTS`로 HTML 브리핑 발송 |
| `--slack` | `.env`의 `SLACK_WEBHOOK_URL`로 요약 전송 |
| `--index` | `output/index.html` 아카이브 인덱스만 갱신 |
| `--log-file` | `logs/` 폴더에 날짜별 로그 파일 저장 |
| `--no-save` | 파일 저장 안 함 (출력만) |

### 자주 쓰는 조합

```bash
# 개발 중 빠른 확인
python marketbrief.py --test --open

# 매일 자동화 배치용
python marketbrief.py --slack --log-file

# 전체 알림 + 기록
python marketbrief.py --email --slack --log-file

# 데이터만 확인 (API 절약)
python marketbrief.py --dry-run
```

---

## 설정 파일 (`config.json`)

Python 코드를 수정하지 않고 동작을 바꿀 수 있습니다.

```jsonc
{
  "model": "claude-opus-4-6",      // 사용할 Claude 모델
  "max_retries": 3,                // API 실패 시 재시도 횟수
  "max_tokens": 2048,              // 최대 응답 토큰 수

  "data": {
    "news_count": 5,               // 수집할 뉴스 헤드라인 수
    "etf_count": 20                // ETF 순위 표시 수
  },

  "output": {
    "save_html": true,             // HTML 리포트 저장 여부
    "auto_update_index": true,     // 아카이브 인덱스 자동 갱신
    "open_browser_after_run": false
  },

  "schedule": {
    "run_at_kst": "06:00",        // 로컬 실행 권장 시각
    "push_send_at_kst": "06:00"   // 푸시 알림 발송 시각
  }
}
```

---

## 환경 변수 (`.env`)

```bash
# 필수
ANTHROPIC_API_KEY=sk-ant-api03-...

# 이메일 발송 (선택)
# Gmail: Google 계정 → 보안 → 앱 비밀번호 발급
EMAIL_SENDER=your@gmail.com
EMAIL_PASSWORD=xxxx xxxx xxxx xxxx
EMAIL_RECIPIENTS=a@example.com,b@example.com

# Slack 알림 (선택)
# Slack → 채널 설정 → Integrations → Incoming Webhooks
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...
```

---

## 출력 파일

### `brief_YYYY-MM-DD.html`
다크 테마 시각화 리포트. 브라우저에서 바로 열어볼 수 있습니다.

- 지수 4개 등락 카드 (색상 코딩)
- 국채 금리 + Fear & Greed 게이지
- 섹터 상승·하락 TOP 3
- 주요 이슈 블릿
- 애널리스트 총평 (Claude 생성)
- LeverageShares ETF TOP 20 테이블

### `index.html`
전체 브리핑 아카이브 목록. 날짜별 S&P 500 / VIX / Fear & Greed 비교 가능.

### `data_YYYY-MM-DD.json`
수집된 raw 시장 데이터. 외부 시스템 연동 시 활용.

### `push_YYYY-MM-DD.json`
FCM / APNs 푸시 알림 페이로드. 모바일 앱 백엔드 연동용.

---

## 알림 설정

### Gmail 앱 비밀번호 발급
1. [Google 계정](https://myaccount.google.com/) → 보안
2. 2단계 인증 활성화
3. 앱 비밀번호 → `메일` / `Windows 컴퓨터` 선택
4. 생성된 16자리를 `EMAIL_PASSWORD`에 입력 (공백 포함 그대로)

### Slack Incoming Webhook
1. [Slack API](https://api.slack.com/apps) → Create New App
2. Incoming Webhooks → 활성화
3. Add New Webhook to Workspace → 채널 선택
4. Webhook URL을 `SLACK_WEBHOOK_URL`에 입력

---

## 개발자 가이드

### 프로젝트 구조

```
marketbrief/
├── marketbrief.py          # 메인 시스템 (~1,365줄)
│   ├── Section 0  환경·로깅 설정
│   ├── Section 1  설정 로더 (config.json)
│   ├── Section 2  MarketDataFetcher 클래스
│   ├── Section 3  NewsFetcher 클래스
│   ├── Section 4  프롬프트 빌더
│   ├── Section 5  Claude API (재시도 로직 포함)
│   ├── Section 6  HTML 리포트 생성기
│   ├── Section 7  이메일 발송
│   ├── Section 8  Slack Webhook
│   ├── Section 9  Push 페이로드
│   ├── Section 10 Mock 데이터
│   ├── Section 11 아카이브 인덱스 생성기
│   └── Section 12 메인 파이프라인 + CLI
│
├── test_marketbrief.py     # 단위 테스트 51개
├── config.json             # 사용자 설정
├── requirements.txt        # 패키지 의존성
├── .env.example            # 환경 변수 템플릿
├── .gitignore
│
├── .github/
│   └── workflows/
│       └── daily_brief.yml # GitHub Actions 워크플로우
│
├── output/                 # 생성된 브리핑 (자동 생성)
└── logs/                   # 실행 로그 (--log-file 사용 시)
```

### 테스트 실행

```bash
# 전체 테스트
pytest test_marketbrief.py -v

# 특정 클래스만
pytest test_marketbrief.py::TestBuildPrompt -v

# 커버리지 포함
pip install pytest-cov
pytest test_marketbrief.py --cov=marketbrief --cov-report=term-missing
```

### 새 데이터 소스 추가

`MarketDataFetcher` 클래스에 메서드를 추가하고 `collect_all()`에서 호출하세요:

```python
def fetch_crypto(self) -> dict:
    """BTC/ETH 가격 추가 예시"""
    result = {}
    for symbol, ticker in {"BTC": "BTC-USD", "ETH": "ETH-USD"}.items():
        hist = yf.Ticker(ticker).history(period="2d")
        # ... 처리
    return result
```

### LeverageShares ETF 티커 업데이트

`config.json`에 `ls_etf_tickers` 배열을 추가하거나 `marketbrief.py`의 `LS_ETF_TICKERS` 리스트를 수정하세요.

---

## 라이선스

내부 사용 전용. LeverageShares 업무 시스템 연동 목적으로 작성됨.
