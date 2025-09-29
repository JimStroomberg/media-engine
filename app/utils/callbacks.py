from __future__ import annotations

import asyncio
import logging
from typing import Optional

import httpx

from ..config import get_settings

logger = logging.getLogger(__name__)


class CallbackDispatcher:
    """Send webhook notifications when jobs finish."""

    def __init__(self) -> None:
        settings = get_settings()
        self.timeout = settings.callback_timeout_seconds
        self.max_attempts = settings.callback_max_attempts
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    async def dispatch(self, url: str, payload) -> None:
        client = await self._get_client()
        for attempt in range(1, self.max_attempts + 1):
            try:
                response = await client.post(url, json=payload.dict())
                response.raise_for_status()
                logger.info("Callback delivered", extra={"url": url})
                return
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Callback attempt failed",
                    extra={"url": url, "attempt": attempt, "error": str(exc)},
                )
                await asyncio.sleep(min(2 ** attempt, 30))
        logger.error("Callback delivery exhausted", extra={"url": url})

    async def shutdown(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None
