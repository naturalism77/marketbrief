@echo off
:: MarketBrief 자동 실행 스크립트
:: Windows 작업 스케줄러에 의해 매일 06:00 KST 실행됨

set PYTHON=C:\Users\jpbom\AppData\Local\Programs\Python\Python311\python.exe
set SCRIPT="D:\leverage shares\업무일지\교육\바이브코딩\시황 코딩\marketbrief.py"
set LOGDIR="D:\leverage shares\업무일지\교육\바이브코딩\시황 코딩\logs"

:: logs 폴더 없으면 생성
if not exist %LOGDIR% mkdir %LOGDIR%

:: 실행 (로그 파일에 결과 기록)
echo [%date% %time%] MarketBrief 시작 >> %LOGDIR%\scheduler.log
%PYTHON% %SCRIPT% >> %LOGDIR%\scheduler.log 2>&1
echo [%date% %time%] MarketBrief 완료 (exit: %errorlevel%) >> %LOGDIR%\scheduler.log
