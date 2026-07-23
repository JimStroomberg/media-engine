from __future__ import annotations

import asyncio
import logging

from .config import get_settings
from .storage.s3 import S3Store


async def initialize_bucket() -> None:
    settings = get_settings()
    if not settings.s3_bucket:
        raise RuntimeError("MEDIA_ENGINE_S3_BUCKET is required")
    store = S3Store(settings)
    await store.ensure_bucket()
    logging.getLogger(__name__).info("S3 bucket is ready", extra={"bucket": store.bucket})


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(initialize_bucket())


if __name__ == "__main__":
    main()
