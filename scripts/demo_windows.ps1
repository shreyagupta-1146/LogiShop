# Opens the four terminals used in the presentation (arrange them next to the browser):
#   1  the application   - every HTTP request, SQL statement, Kafka message and consumer decision
#   2  the DB server log - what PostgreSQL itself received, tagged with the sending component
#   3  the change feed   - every row INSERT/UPDATE/DELETE as it is committed (LISTEN/NOTIFY)
#   4  psql              - type your own SQL
$root = Split-Path $PSScriptRoot -Parent
function Open-Term($title, $cmd) {
    Start-Process powershell -ArgumentList "-NoExit", "-Command", "`$Host.UI.RawUI.WindowTitle = '$title'; Set-Location '$root'; $cmd"
}
& "$PSScriptRoot\start_db.ps1"
Open-Term "1 APP (python run.py)"        "`$env:NO_BROWSER = '1'; python run.py"
Open-Term "2 POSTGRESQL SERVER LOG"      "python db_log.py"
Open-Term "3 DATABASE CHANGE FEED"       "python db_watch.py --hide ml_prediction gps_ping"
Open-Term "4 psql (demo_viewer)"         ".\scripts\psql.ps1"

Write-Host "waiting for the application ..." -NoNewline
for ($i = 0; $i -lt 90; $i++) {
    try { if ((Invoke-WebRequest -Uri "http://127.0.0.1:8765/api/health" -UseBasicParsing -TimeoutSec 2).StatusCode -eq 200) { break } } catch { }
    Start-Sleep -Seconds 1; Write-Host "." -NoNewline
}
Write-Host ""
Start-Process "http://127.0.0.1:8765"
Start-Process "http://127.0.0.1:5050"
Write-Host "Shop: http://127.0.0.1:8765   Back office: /admin   Driver: /driver   pgAdmin: http://127.0.0.1:5050"
