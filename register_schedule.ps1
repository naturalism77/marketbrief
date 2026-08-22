# MarketBrief 스케줄 등록 (PowerShell)
# 반드시 관리자 권한으로 실행: 우클릭 → "PowerShell로 실행" 또는 "관리자 권한으로 실행"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$PythonExe = (Get-Command python).Source

# logs 폴더 생성
$LogDir = Join-Path $ScriptDir "logs"
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir | Out-Null }

$TaskAction = New-ScheduledTaskAction `
    -Execute $PythonExe `
    -Argument "-X utf8 `"$ScriptDir\marketbrief.py`" --log-file" `
    -WorkingDirectory $ScriptDir

$TriggerMorning = New-ScheduledTaskTrigger -Daily -At "06:00"
$TriggerEvening = New-ScheduledTaskTrigger -Daily -At "18:00"

$Settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30) `
    -RestartCount 2 `
    -RestartInterval (New-TimeSpan -Minutes 5) `
    -StartWhenAvailable

# 오전 6시 태스크 등록
Unregister-ScheduledTask -TaskName "MarketBrief_Morning" -Confirm:$false -ErrorAction SilentlyContinue
Register-ScheduledTask `
    -TaskName "MarketBrief_Morning" `
    -Action $TaskAction `
    -Trigger $TriggerMorning `
    -Settings $Settings `
    -RunLevel Highest `
    -Description "MarketBrief 일일 시황 - 오전 6시 (미국 장 마감 후)" `
    -Force | Out-Null

if ($?) {
    Write-Host "[성공] MarketBrief_Morning 등록 완료 (매일 06:00)" -ForegroundColor Green
} else {
    Write-Host "[실패] MarketBrief_Morning 등록 실패" -ForegroundColor Red
}

# 오후 6시 태스크 등록
Unregister-ScheduledTask -TaskName "MarketBrief_Evening" -Confirm:$false -ErrorAction SilentlyContinue
Register-ScheduledTask `
    -TaskName "MarketBrief_Evening" `
    -Action $TaskAction `
    -Trigger $TriggerEvening `
    -Settings $Settings `
    -RunLevel Highest `
    -Description "MarketBrief 일일 시황 - 오후 6시 (미국 장 개장 전)" `
    -Force | Out-Null

if ($?) {
    Write-Host "[성공] MarketBrief_Evening 등록 완료 (매일 18:00)" -ForegroundColor Green
} else {
    Write-Host "[실패] MarketBrief_Evening 등록 실패" -ForegroundColor Red
}

Write-Host ""
Write-Host "등록된 스케줄:" -ForegroundColor Cyan
Get-ScheduledTask -TaskName "MarketBrief_*" | Select-Object TaskName, State | Format-Table -AutoSize

Write-Host "완료. 이 창을 닫아도 됩니다." -ForegroundColor Yellow
Read-Host "Enter 키를 누르면 종료"
