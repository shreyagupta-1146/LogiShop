# Stops the application and the Docker containers (the database keeps its data in a Docker volume).
$c = Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue
if ($c) { Stop-Process -Id $c.OwningProcess -Force; "application stopped" }
Set-Location (Split-Path $PSScriptRoot -Parent)
docker compose -f deploy/docker-compose.yml stop
