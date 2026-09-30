# trip-rag 매일 수집을 작업 스케줄러에 등록한다. PowerShell 에서 한 번만 실행:
#   powershell -ExecutionPolicy Bypass -File scripts\register_task.ps1
# 시간을 바꾸려면:  ... -File scripts\register_task.ps1 -At 13:10
param([string]$At = "10:10", [string]$Areas = "6")

$ps1 = Join-Path $PSScriptRoot "daily_collect.ps1"
# conhost --headless: 기본 터미널이 Windows Terminal 이어도 창을 띄우지 않는다
$action = New-ScheduledTaskAction -Execute "conhost.exe" `
    -Argument "--headless powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$ps1`" -Areas $Areas"
$trigger = New-ScheduledTaskTrigger -Daily -At $At
# StartWhenAvailable: 그 시간에 PC 가 꺼져 있었으면 켜진 뒤 바로 한 번 돈다
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 2)

Register-ScheduledTask -TaskName "trip-rag 수집" -Action $action -Trigger $trigger -Settings $settings `
    -Description "TourAPI 하루 한도(950회) 안에서 상세·소개정보를 이어서 수집" -Force | Out-Null

Write-Host "등록 완료: 매일 $At, 지역 $Areas"
Write-Host "지금 바로 한 번 돌려보기:  Start-ScheduledTask -TaskName 'trip-rag 수집'"
Write-Host "로그:  logs\collect_$(Get-Date -Format yyyyMMdd).log"
