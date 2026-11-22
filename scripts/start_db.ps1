# Starts the PostgreSQL server (and pgAdmin) in Docker and waits until the database answers.
Set-Location (Split-Path $PSScriptRoot -Parent)
docker compose -f deploy/docker-compose.yml up -d
Write-Host "waiting for PostgreSQL ..." -NoNewline
for ($i = 0; $i -lt 40; $i++) {
    $h = docker inspect -f "{{.State.Health.Status}}" logistics-db 2>$null
    if ($h -eq "healthy") { break }
    Start-Sleep -Seconds 2; Write-Host "." -NoNewline
}
Write-Host ""
Write-Host "PostgreSQL : 127.0.0.1:5433   database 'logistics'   (container logistics-db)"
Write-Host "pgAdmin    : http://127.0.0.1:5050   (servers are pre-registered, no login)"
