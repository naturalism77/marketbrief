@echo off
chcp 65001 >nul
echo.
echo ====================================================
echo  MarketBrief — Python 자동 설치 및 실행
echo ====================================================
echo.

:: ── Step 1: Python 확인 ──────────────────────────────────────
python --version >nul 2>&1
if %errorlevel% == 0 (
    echo [OK] Python이 이미 설치되어 있습니다.
    python --version
    goto :install_deps
)

:: ── Step 2: winget으로 Python 설치 ──────────────────────────
echo [1/3] winget으로 Python 3.11 설치 중...
echo       (Windows 스토어를 통해 자동 설치됩니다. 잠시 기다려 주세요.)
winget install --id Python.Python.3.11 --silent --accept-source-agreements --accept-package-agreements

if %errorlevel% neq 0 (
    echo.
    echo [대안] winget 설치 실패. 아래 방법 중 하나를 시도하세요:
    echo.
    echo   방법 1 - Microsoft Store:
    echo     시작 메뉴 → Microsoft Store → "Python 3.11" 검색 → 설치
    echo.
    echo   방법 2 - 공식 사이트:
    echo     https://www.python.org/downloads/
    echo     설치 시 "Add Python to PATH" 반드시 체크!
    echo.
    pause
    exit /b 1
)

:: PATH 갱신 (새 설치 후 현재 세션에 반영)
set "PATH=%LOCALAPPDATA%\Programs\Python\Python311;%LOCALAPPDATA%\Programs\Python\Python311\Scripts;%PATH%"
set "PATH=%APPDATA%\Python\Python311\Scripts;%PATH%"

:: 재확인
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo.
    echo [안내] Python이 설치됐지만 이 창에서 인식되지 않습니다.
    echo        이 창을 닫고 새 터미널에서 다시 실행하세요.
    pause
    exit /b 1
)
echo [OK] Python 설치 완료:
python --version

:install_deps
:: ── Step 3: 패키지 설치 ─────────────────────────────────────
echo.
echo [2/3] 패키지 설치 중...
cd /d "%~dp0"
python -m pip install --upgrade pip -q
python -m pip install -r requirements.txt -q
if %errorlevel% neq 0 (
    echo [오류] 패키지 설치 실패
    pause
    exit /b 1
)
echo [OK] 패키지 설치 완료

:: ── Step 4: .env 확인 ────────────────────────────────────────
if not exist ".env" (
    echo.
    copy ".env.example" ".env" >nul
    echo [안내] .env 파일 생성됨. API 키를 입력하세요:
    notepad .env
    echo.
    echo .env 저장 후 아무 키나 누르면 계속합니다...
    pause >nul
)

:: ── Step 5: 테스트 실행 ──────────────────────────────────────
echo.
echo [3/3] 단위 테스트 실행 중 (72개)...
python -m pytest test_marketbrief.py -v --tb=short 2>&1

if %errorlevel% neq 0 (
    echo.
    echo [오류] 테스트 실패. 위 오류 메시지를 확인하세요.
    pause
    exit /b 1
)

:: ── Step 6: 파이프라인 검증 ──────────────────────────────────
echo.
echo 테스트 통과! Mock 파이프라인 검증 + HTML 미리보기...
python marketbrief.py --test --open

echo.
echo ====================================================
echo  설치 및 검증 완료!
echo.
echo  실제 시황 생성:
echo    python marketbrief.py
echo.
echo  자동화 (매일 06:00):
echo    schedule_task.bat  (관리자 권한 실행)
echo ====================================================
pause
