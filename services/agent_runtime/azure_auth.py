"""
Azure authentication, one place for every Azure call.

Preferred: Microsoft Entra ID via DefaultAzureCredential. On the Azure VM this picks up the
VM's managed identity automatically (no secrets on disk); locally it uses `az login`.
Fallback: an API key from the environment (use Key Vault in production).
"""
from __future__ import annotations

import time
from typing import Optional

from azure.identity.aio import DefaultAzureCredential


class AzureAuth:
    """Returns request headers for one Azure service; caches Entra tokens until near expiry."""

    def __init__(self, scope: str, api_key: str = "", key_header: str = "api-key") -> None:
        self.scope, self.api_key, self.key_header = scope, api_key, key_header
        self._credential: Optional[DefaultAzureCredential] = None
        self._token: Optional[str] = None
        self._expires_on = 0.0

    async def headers(self) -> dict[str, str]:
        if self.api_key:
            return {self.key_header: self.api_key}
        if not self._token or time.time() > self._expires_on - 300:  # refresh 5 minutes early
            if self._credential is None:
                self._credential = DefaultAzureCredential()
            token = await self._credential.get_token(self.scope)
            self._token, self._expires_on = token.token, float(token.expires_on)
        return {"Authorization": f"Bearer {self._token}"}

    @property
    def mode(self) -> str:
        return "api-key" if self.api_key else "entra-id"

    async def close(self) -> None:
        if self._credential is not None:
            await self._credential.close()
