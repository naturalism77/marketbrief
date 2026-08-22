@echo off
chcp 65001 >nul

:: output\ 폴더에서 가장 최근 HTML 브리핑을 브라우저로 열기
set OUTPUT_DIR=%~dp0output

if not exist "%OUTPUT_DIR%" (
    echo [안내] output\ 폴더가 없습니다. 먼저 python marketbrief.py 를 실행하세요.
    pause
    exit /b 1
)

:: 날짜 정렬 기준 최신 파일 탐색
for /f "delims=" %%f in ('dir /b /o-n "%OUTPUT_DIR%\brief_*.html" 2^>nul') do (
    set LATEST=%%f
    goto :open
)

echo [안내] 생성된 HTML 브리핑이 없습니다.
pause
exit /b 1

:open
echo 최신 브리핑 열기: %LATEST%
start "" "%OUTPUT_DIR%\%LATEST%"
