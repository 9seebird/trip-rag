# trip-rag 매일 수집. 작업 스케줄러가 부른다 (등록은 register_task.ps1).
# Docker Desktop 이 꺼져 있으면 켜고 기다린 뒤, 수집을 돌리고 logs\collect_날짜.log 에 남긴다.
param([string]$Areas = "6")          # 6=부산. 여러 곳이면 "6,32"

# 한글 깨짐 방지: 파이썬이 내보내는 UTF-8 을 UTF-8 로 읽게 한다 (기본은 CP949 로 읽어서 깨짐)
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
New-Item -ItemType Directory -Force (Join-Path $root "logs") | Out-Null
$log = Join-Path $root ("logs\collect_" + (Get-Date -Format yyyyMMdd) + ".log")
function Log([string]$m) { "[$(Get-Date -Format HH:mm:ss)] $m" | Out-File $log -Append -Encoding utf8 }
function Run([string]$cmd) {
    Log "> $cmd"
    cmd /c "chcp 65001 >nul & $cmd 2>&1" | Out-File $log -Append -Encoding utf8
    return $LASTEXITCODE
}

# scripts\areas.txt 가 있으면 그 값을 쓴다. 지역을 늘릴 때 작업을 다시 등록할 필요 없이 이 파일만 고치면 된다.
$areasFile = Join-Path $PSScriptRoot "areas.txt"
if (Test-Path $areasFile) {
    $fromFile = ((Get-Content $areasFile | Where-Object { $_ -notmatch '^\s*#' }) -join ",") -replace '\s', ''
    $fromFile = $fromFile.Trim(',') -replace ',+', ','
    if ($fromFile) { $Areas = $fromFile }
}

Log "시작 (지역 $Areas)"

cmd /c "docker info >nul 2>&1"
if ($LASTEXITCODE -ne 0) {
    Log "Docker Desktop 이 꺼져 있어 켭니다"
    $dd = Join-Path $env:ProgramFiles "Docker\Docker\Docker Desktop.exe"
    if (Test-Path $dd) { Start-Process $dd }
    for ($i = 0; $i -lt 36; $i++) {
        Start-Sleep -Seconds 5
        cmd /c "docker info >nul 2>&1"
        if ($LASTEXITCODE -eq 0) { break }
    }
    if ($LASTEXITCODE -ne 0) { Log "3분 기다려도 Docker 가 안 떠서 중단"; exit 1 }
}

Run "docker compose up -d db" | Out-Null
$code = Run "docker compose --profile tools run --rm -e TOUR_DAILY_LIMIT=950 -e PYTHONIOENCODING=utf-8 ingest python -m ingest.daily --areas $Areas"
Log "끝 (종료코드 $code)"
exit $code
