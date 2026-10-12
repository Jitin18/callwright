"""
MCP client hub. One persistent MCP session per scope, opened at startup.
Subagents never get the hub directly — they get a ScopedToolbox limited to their scopes.
"""
from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from fastmcp import Client

from .config import settings

SCOPES = ("member_identity", "loyalty_accounts", "member_enrolment", "case_management", "knowledge_base",
          "order_verification", "order_service")


class McpHub:
    def __init__(self, base_url: str | None = None) -> None:
        self.base_url = (base_url or settings.mcp_gateway_url).rstrip("/")
        self._clients: dict[str, Client] = {}
        self._stack = contextlib.AsyncExitStack()

    async def start(self, wait_s: float = 30) -> None:
        """Connect to every scope. Waits for the gateway so services can start in any order."""
        deadline = asyncio.get_running_loop().time() + wait_s
        for scope in SCOPES:
            while True:
                client = Client(f"{self.base_url}/{scope}/mcp")
                try:
                    await self._stack.enter_async_context(client)
                    break
                except Exception:
                    if asyncio.get_running_loop().time() > deadline:
                        raise RuntimeError(f"MCP gateway not reachable at {self.base_url} — is it running?")
                    await asyncio.sleep(1)
            self._clients[scope] = client

    async def stop(self) -> None:
        await self._stack.aclose()

    async def call(self, scope: str, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        client = self._clients[scope]
        clean = {k: v for k, v in args.items() if v is not None}
        try:
            result = await asyncio.wait_for(client.call_tool(tool, clean, raise_on_error=False),
                                            timeout=settings.mcp_timeout_s)
        except asyncio.TimeoutError:
            return {"error": True, "status_code": 504, "detail": f"{scope}.{tool} timed out"}
        if result.is_error:
            text = " ".join(getattr(c, "text", "") for c in result.content)
            return {"error": True, "detail": text or "tool error"}
        data = result.structured_content
        if isinstance(data, dict) and set(data) == {"result"}:
            data = data["result"]
        return data if isinstance(data, dict) else {"result": data}


class ScopedToolbox:
    """Least-privilege view of the hub for one subagent."""

    def __init__(self, hub: McpHub, scopes: tuple[str, ...], owner: str) -> None:
        self.hub, self.scopes, self.owner = hub, scopes, owner

    async def call(self, scope: str, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        if scope not in self.scopes:
            raise PermissionError(f"{self.owner} is not permitted to use MCP scope '{scope}'")
        return await self.hub.call(scope, tool, args)
