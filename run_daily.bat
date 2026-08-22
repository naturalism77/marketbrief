@echo off
chcp 65001 >nul

:: ====================================================
:: MarketBrief — 일일 시황 생성 실행 스크립트
:: Windows 작업 스케줄러에 등록하여 자동 실행 가능
:: 권장 실행 시각: 06:00 KST (미국 증시 마감 후)
:: ====================================================

set SCRIPT_DIR=%~dp0
cd /d "%SCRIPT_DIR%"

echo [%date% %time%] MarketBrief 시작
python -X utf8 marketbrief.py --log-file 2>&1

if errorlevel 1 (
    echo [%date% %time%] 오류 발생 — logs\marketbrief.log 확인
) else (
    echo [%date% %time%] 완료
)
