# Opens psql inside the database container.  Read-only by default; use -User app_user to be able to write.
param([string]$User = "demo_viewer")
Set-Location (Split-Path $PSScriptRoot -Parent)
docker cp deploy/psqlrc logistics-db:/tmp/psqlrc | Out-Null
$env:PGPASSWORD = if ($User -eq "app_user") { "app" } elseif ($User -eq "postgres") { "postgres" } else { "demo" }
docker exec -it -e PSQLRC=/tmp/psqlrc -e PGPASSWORD=$env:PGPASSWORD logistics-db psql -U $User -d logistics
