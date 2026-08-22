@echo off
chcp 65001 >nul
echo.
echo ====================================================
echo  MarketBrief — 초기 설치 스크립트
echo ====================================================
echo.

:: Python 확인
python --version >nul 2>&1
if errorlevel 1 (
    echo [오류] Python이 설치되지 않았습니다.
    echo https://www.python.org/downloads/ 에서 설치 후 다시 실행하세요.
    pause
    exit /b 1
)

echo [1/3] Python 버전:
python --version

echo.
echo [2/3] 패키지 설치 중...
python -m pip install -r requirements.txt

echo.
echo [3/3] .env 파일 확인...
if not exist ".env" (
    copy ".env.example" ".env"
    echo [안내] .env 파일이 생성됐습니다.
    echo       메모장으로 .env를 열어 ANTHROPIC_API_KEY를 입력하세요.
    notepad .env
) else (
    echo [OK] .env 파일이 이미 존재합니다.
)

echo.
echo ====================================================
echo  설치 완료! 아래 명령어로 테스트하세요:
echo.
echo    python marketbrief.py --dry-run
echo    python marketbrief.py --show-data
echo    python marketbrief.py
echo ====================================================
echo.
pause
