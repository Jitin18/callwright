# Stops the services started by run_all.ps1 (by port).
foreach ($port in 8000, 8100, 8200, 5173) {
    Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue |
        ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue; Write-Host "Stopped :$port" }
}
