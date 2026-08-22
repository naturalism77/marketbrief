@echo off
chcp 65001 >nul
echo.
echo ====================================================
echo  MarketBrief — Windows 작업 스케줄러 등록
echo  매일 06:00 KST (미국 장 마감 후)
echo  매일 18:00 KST (다음날 미국 장 개장 전)
echo ====================================================
echo.

:: 관리자 권한 확인
net session >nul 2>&1
if errorlevel 1 (
    echo [오류] 관리자 권한으로 실행하세요.
    echo 이 파일을 우클릭 → "관리자 권한으로 실행"
    pause
    exit /b 1
)

set SCRIPT_DIR=%~dp0
set BAT_PATH=%SCRIPT_DIR%run_daily.bat

:: logs 폴더 생성
if not exist "%SCRIPT_DIR%logs" mkdir "%SCRIPT_DIR%logs"

:: ── 오전 6시 태스크 ──
set TASK_MORNING=MarketBrief_Morning
schtasks /delete /tn "%TASK_MORNING%" /f >nul 2>&1
schtasks /delete /tn "MarketBrief_Daily" /f >nul 2>&1

schtasks /create ^
  /tn "%TASK_MORNING%" ^
  /tr "\"%BAT_PATH%\"" ^
  /sc daily ^
  /st 06:00 ^
  /ru "%USERNAME%" ^
  /rl highest ^
  /f

if errorlevel 1 (
    echo [오류] 오전 6시 작업 등록 실패
) else (
    echo [성공] 오전 6시 작업 등록됨: %TASK_MORNING%
)

:: ── 오후 6시 태스크 ──
set TASK_EVENING=MarketBrief_Evening
schtasks /delete /tn "%TASK_EVENING%" /f >nul 2>&1

schtasks /create ^
  /tn "%TASK_EVENING%" ^
  /tr "\"%BAT_PATH%\"" ^
  /sc daily ^
  /st 18:00 ^
  /ru "%USERNAME%" ^
  /rl highest ^
  /f

if errorlevel 1 (
    echo [오류] 오후 6시 작업 등록 실패
) else (
    echo [성공] 오후 6시 작업 등록됨: %TASK_EVENING%
)

echo.
echo ====================================================
echo  등록된 스케줄:
echo    MarketBrief_Morning  : 매일 06:00 KST
echo    MarketBrief_Evening  : 매일 18:00 KST
echo.
echo  확인: 작업 스케줄러 앱에서 위 이름 검색
echo ====================================================
echo.
pause
