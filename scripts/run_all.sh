#!/usr/bin/env bash
# Starts backend (8100), MCP gateway (8200), agent runtime (8000) and the Vite dev UI (5173).
set -euo pipefail
cd "$(dirname "$0")/.."
# tr strips CRLF line endings (.env edited on Windows)
[ -f .env ] && set -a && . <(tr -d '\r' < .env) && set +a
trap 'kill 0' EXIT
python -m uvicorn services.mock_backend.app:app --port 8100 &
python -m uvicorn services.mcp_gateway.server:app --port 8200 &
python -m uvicorn services.agent_runtime.app:app --port 8000 &
(cd frontend && npm run dev) &
wait
