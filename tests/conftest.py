import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _wait(port: int, timeout: float = 15) -> None:
    end = time.time() + timeout
    while time.time() < end:
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.2)
    raise RuntimeError(f"port {port} did not open")


@pytest.fixture(scope="session")
def services():
    """Starts the real mock backend and real MCP gateway (no LLM involved)."""
    env = {**os.environ, "MOCK_LATENCY_MS_MIN": "0", "MOCK_LATENCY_MS_MAX": "5",
           "BACKEND_URL": "http://127.0.0.1:18100", "BACKEND_TIMEOUT_S": "1.5"}
    be = subprocess.Popen([sys.executable, "-m", "uvicorn", "services.mock_backend.app:app", "--port", "18100"],
                          cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    gw = subprocess.Popen([sys.executable, "-m", "uvicorn", "services.mcp_gateway.server:app", "--port", "18200"],
                          cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        _wait(18100, timeout=180)  # the backend loads the RAG embedding model before it listens
        _wait(18200)
        yield {"backend": "http://127.0.0.1:18100", "gateway": "http://127.0.0.1:18200"}
    finally:
        be.terminate()
        gw.terminate()
