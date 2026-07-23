from __future__ import annotations

import asyncio
import logging

import httpx

from .config import get_settings
from .persistence.database import check_database, close_database, configure_database, get_session_factory
from .security import CredentialCipher
from .services.webhooks import WebhookDispatcher

logger = logging.getLogger(__name__)


async def run_dispatcher() -> None:
    settings = get_settings()
    if not settings.database_url:
        raise RuntimeError("Webhook dispatcher requires MEDIA_ENGINE_DATABASE_URL")
    if settings.credential_encryption_key is None:
        raise RuntimeError(
            "Webhook dispatcher requires MEDIA_ENGINE_CREDENTIAL_ENCRYPTION_KEY. Add it and retry startup."
        )

    configure_database(settings.database_url)
    await check_database()
    cipher = CredentialCipher(settings.credential_encryption_key.get_secret_value())
    timeout = httpx.Timeout(settings.webhook_timeout_seconds)
    limits = httpx.Limits(max_connections=20, max_keepalive_connections=10)
    async with httpx.AsyncClient(
        timeout=timeout,
        limits=limits,
        follow_redirects=False,
    ) as client:
        dispatcher = WebhookDispatcher(
            settings,
            cipher,
            get_session_factory(),
            client,
        )
        await dispatcher.validate_credentials()
        logger.info("Webhook dispatcher is ready")
        try:
            while True:
                handled = await dispatcher.dispatch_once()
                if not handled:
                    await asyncio.sleep(settings.webhook_dispatch_interval_seconds)
        finally:
            await close_database()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_dispatcher())


if __name__ == "__main__":
    main()
