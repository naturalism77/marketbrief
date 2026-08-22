"""Helper script to register Windows Task Scheduler tasks for MarketBrief."""
import subprocess
import sys
import os
import tempfile
from pathlib import Path

script_dir = str(Path(__file__).parent)
python_exe = sys.executable

ps_content = f"""
$ScriptDir = "{script_dir}"
$PythonExe = "{python_exe}"

$TaskAction = New-ScheduledTaskAction `
    -Execute $PythonExe `
    -Argument "-X utf8 '$ScriptDir\\marketbrief.py' --log-file" `
    -WorkingDirectory $ScriptDir

$TriggerMorning = New-ScheduledTaskTrigger -Daily -At '06:00'
$TriggerEvening = New-ScheduledTaskTrigger -Daily -At '18:00'

$Settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30) `
    -StartWhenAvailable `
    -RestartCount 2 `
    -RestartInterval (New-TimeSpan -Minutes 5)

# 기존 태스크 제거
Unregister-ScheduledTask -TaskName 'MarketBrief_Daily'   -Confirm:$false -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName 'MarketBrief_Morning' -Confirm:$false -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName 'MarketBrief_Evening' -Confirm:$false -ErrorAction SilentlyContinue

# 오전 6시 등록
Register-ScheduledTask `
    -TaskName 'MarketBrief_Morning' `
    -Action $TaskAction `
    -Trigger $TriggerMorning `
    -Settings $Settings `
    -Description 'MarketBrief 오전 6시 자동 업데이트 (미국 장 마감 후)' `
    -Force

# 오후 6시 등록
Register-ScheduledTask `
    -TaskName 'MarketBrief_Evening' `
    -Action $TaskAction `
    -Trigger $TriggerEvening `
    -Settings $Settings `
    -Description 'MarketBrief 오후 6시 자동 업데이트 (미국 장 개장 전)' `
    -Force

Write-Host "등록된 스케줄:"
Get-ScheduledTask -TaskName 'MarketBrief_*' | Select-Object TaskName, State | Format-Table
"""

with tempfile.NamedTemporaryFile(mode='w', suffix='.ps1', delete=False, encoding='utf-8') as f:
    f.write(ps_content)
    tmp_path = f.name

print(f"PS1 임시 파일: {tmp_path}")
result = subprocess.run(
    ['powershell', '-ExecutionPolicy', 'Bypass', '-File', tmp_path],
    capture_output=True, text=True, encoding='utf-8', errors='replace'
)
print("STDOUT:", result.stdout)
if result.stderr:
    print("STDERR:", result.stderr[:500])
print("Return code:", result.returncode)
os.unlink(tmp_path)
