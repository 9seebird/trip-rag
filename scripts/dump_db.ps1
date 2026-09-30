# 집 PC 의 DB 를 통째로 파일로 뺀다 → 서버로 올려서 restore.
#   powershell -ExecutionPolicy Bypass -File scripts\dump_db.ps1
# PowerShell 의 > 리다이렉트는 바이너리를 깨뜨리므로, 컨테이너 안에서 파일로 만든 뒤 docker cp 로 꺼낸다.
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$name = "triprag_" + (Get-Date -Format yyyyMMdd) + ".dump"
docker compose up -d db | Out-Null
docker compose exec -T db pg_dump -U triprag -d triprag -Fc -f /tmp/$name
docker cp triprag-db:/tmp/$name (Join-Path $root $name)
docker compose exec -T db rm /tmp/$name
Write-Host "완료: $name  ($([math]::Round((Get-Item $name).Length/1MB,1)) MB)"
Write-Host "서버로:  scp $name <사용자>@<서버>:~/trip-rag/"
