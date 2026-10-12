# Starts the mock backend (8100), MCP gateway (8200) and agent runtime (8000) on Windows.
# The runtime also serves the built UI at http://localhost:8000 (the browser mic needs localhost or HTTPS).
#
#   .\scripts\run_all.ps1            build the UI if needed, start everything, logs in .\logs
#   .\scripts\run_all.ps1 -Dev       also start the Vite dev server on http://localhost:5173
#   .\scripts\stop_all.ps1           stop everything
param([switch]$Dev)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { throw "No .venv found. Run: uv venv .venv --python 3.12; uv pip install --python .venv\Scripts\python.exe -r requirements.txt" }
if (-not (Test-Path ".env")) { Copy-Item ".env.example" ".env"; Write-Host "Created .env from .env.example: set the Voice Live endpoint and key." }
New-Item -ItemType Directory -Force logs | Out-Null
$env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' + [Environment]::GetEnvironmentVariable('Path', 'User')

if (-not (Test-Path "frontend\dist\index.html")) {
    Write-Host "Building the UI..."
    Push-Location frontend
    if (-not (Test-Path "node_modules")) { npm install --no-audit --no-fund }
    npm run build
    Pop-Location
}

function Start-Service($name, $module, $port) {
    $busy = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    if ($busy) { Write-Host "$name already running on :$port"; return }
    Start-Process -FilePath $py -ArgumentList "-m", "uvicorn", $module, "--host", "0.0.0.0", "--port", $port, "--env-file", ".env" `
        -WorkingDirectory $root -WindowStyle Hidden `
        -RedirectStandardOutput "logs\$name.out.log" -RedirectStandardError "logs\$name.log" | Out-Null
    Write-Host "Started $name on :$port (logs\$name.log)"
}

function Wait-Port($port, $seconds) {
    $deadline = (Get-Date).AddSeconds($seconds)
    while ((Get-Date) -lt $deadline) {
        if (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue) { return }
        Start-Sleep -Milliseconds 500
    }
    throw "Port $port did not open; check the logs folder."
}

Start-Service "backend" "services.mock_backend.app:app" 8100
Start-Service "gateway" "services.mcp_gateway.server:app" 8200
Wait-Port 8100 180   # the backend loads the RAG embedding model first
Wait-Port 8200 30
Start-Service "runtime" "services.agent_runtime.app:app" 8000
Wait-Port 8000 60

if ($Dev) {
    Start-Process -FilePath "cmd.exe" -ArgumentList "/c", "npm run dev > ..\logs\ui.log 2>&1" -WorkingDirectory "$root\frontend" -WindowStyle Hidden | Out-Null
    Write-Host "Vite dev UI on http://localhost:5173"
}
Write-Host "Ready: open http://localhost:8000"
