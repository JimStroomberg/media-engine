from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from openai import AsyncOpenAI
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings
from ..domain.pipelines import UNDERSTAND_PROVIDER_MODELS
from ..persistence.models import ProviderConfig
from ..security import CredentialCipher, credential_hint

SUPPORTED_PROVIDERS = frozenset(UNDERSTAND_PROVIDER_MODELS)
PROVIDER_BASE_URLS = {
    "openai": "https://api.openai.com/v1",
    "xai": "https://api.x.ai/v1",
}
PROVIDER_MODELS = {
    provider: dict(models)
    for provider, models in UNDERSTAND_PROVIDER_MODELS.items()
}


class ProviderUnavailable(RuntimeError):
    pass


class ProviderConflict(RuntimeError):
    pass


class ProviderVerificationFailed(RuntimeError):
    pass


@dataclass(frozen=True)
class RuntimeProviderConnection:
    provider_config_id: uuid.UUID
    provider: str
    api_key: str
    base_url: str
    models: dict[str, str]
    timeout_seconds: float
    max_retries: int


class ProviderConfigurationService:
    def __init__(self, settings: Settings, cipher: CredentialCipher) -> None:
        self.settings = settings
        self.cipher = cipher

    async def list(self, session: AsyncSession) -> list[ProviderConfig]:
        return list(await session.scalars(select(ProviderConfig).order_by(ProviderConfig.provider)))

    async def get(self, session: AsyncSession, provider_config_id: uuid.UUID) -> ProviderConfig | None:
        return await session.get(ProviderConfig, provider_config_id)

    async def create(
        self,
        session: AsyncSession,
        *,
        name: str,
        provider: str,
        api_key: str,
        base_url: str | None,
        models: dict[str, str] | None,
        timeout_seconds: float,
        max_retries: int,
        enabled: bool,
        is_default: bool,
        verify: bool = True,
    ) -> ProviderConfig:
        if provider not in SUPPORTED_PROVIDERS:
            raise ValueError(f"Unsupported provider {provider}")
        existing = await session.scalar(select(ProviderConfig).where(ProviderConfig.provider == provider))
        if existing is not None:
            raise ProviderConflict(f"A {provider} provider connection already exists")
        if await session.scalar(select(ProviderConfig.id).where(ProviderConfig.name == name)):
            raise ProviderConflict(f"Provider connection {name!r} already exists")

        resolved_base_url = (base_url or PROVIDER_BASE_URLS[provider]).rstrip("/")
        resolved_models = dict(PROVIDER_MODELS[provider])
        resolved_models.update(models or {})
        last_verified_at = None
        last_error = None
        if verify:
            await self.verify_credentials(provider=provider, api_key=api_key, base_url=resolved_base_url)
            last_verified_at = datetime.now(UTC)

        if is_default:
            enabled = True
            await self._clear_default(session)
        elif enabled and not await self._has_default(session):
            is_default = True

        config = ProviderConfig(
            id=uuid.uuid4(),
            name=name,
            provider=provider,
            encrypted_api_key=self.cipher.encrypt(api_key),
            credential_hint=credential_hint(api_key),
            base_url=resolved_base_url,
            models=resolved_models,
            runtime_options={"timeout_seconds": timeout_seconds, "max_retries": max_retries},
            enabled=enabled,
            is_default=is_default,
            last_verified_at=last_verified_at,
            last_error=last_error,
        )
        session.add(config)
        await session.commit()
        await session.refresh(config)
        return config

    async def update(
        self,
        session: AsyncSession,
        config: ProviderConfig,
        *,
        name: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        models: dict[str, str] | None = None,
        timeout_seconds: float | None = None,
        max_retries: int | None = None,
        enabled: bool | None = None,
        is_default: bool | None = None,
        verify: bool = True,
    ) -> ProviderConfig:
        resolved_key = api_key or self.cipher.decrypt(config.encrypted_api_key)
        resolved_base_url = (base_url or config.base_url).rstrip("/")
        if name is not None and await session.scalar(
            select(ProviderConfig.id).where(
                ProviderConfig.name == name,
                ProviderConfig.id != config.id,
            )
        ):
            raise ProviderConflict(f"Provider connection {name!r} already exists")
        if verify and (api_key is not None or base_url is not None):
            await self.verify_credentials(
                provider=config.provider,
                api_key=resolved_key,
                base_url=resolved_base_url,
            )
            config.last_verified_at = datetime.now(UTC)
            config.last_error = None

        if name is not None:
            config.name = name
        if api_key is not None:
            config.encrypted_api_key = self.cipher.encrypt(api_key)
            config.credential_hint = credential_hint(api_key)
        if base_url is not None:
            config.base_url = resolved_base_url
        if models is not None:
            merged = dict(config.models)
            merged.update(models)
            config.models = merged
        runtime_options = dict(config.runtime_options)
        if timeout_seconds is not None:
            runtime_options["timeout_seconds"] = timeout_seconds
        if max_retries is not None:
            runtime_options["max_retries"] = max_retries
        config.runtime_options = runtime_options
        if enabled is not None:
            config.enabled = enabled
            if not enabled:
                config.is_default = False
        if is_default:
            await self._clear_default(session)
            config.is_default = True
            config.enabled = True
        elif is_default is False:
            config.is_default = False

        await session.flush()
        if not await self._has_default(session):
            replacement = await session.scalar(
                select(ProviderConfig)
                .where(ProviderConfig.enabled.is_(True), ProviderConfig.id != config.id)
                .order_by((ProviderConfig.provider == "xai").desc(), ProviderConfig.created_at)
            )
            if replacement is not None:
                replacement.is_default = True
            elif config.enabled:
                config.is_default = True
        await session.commit()
        await session.refresh(config)
        return config

    async def delete(self, session: AsyncSession, config: ProviderConfig) -> None:
        was_default = config.is_default
        await session.delete(config)
        await session.flush()
        if was_default:
            replacement = await session.scalar(
                select(ProviderConfig)
                .where(ProviderConfig.enabled.is_(True))
                .order_by((ProviderConfig.provider == "xai").desc(), ProviderConfig.created_at)
            )
            if replacement is not None:
                replacement.is_default = True
        await session.commit()

    async def verify(self, session: AsyncSession, config: ProviderConfig) -> ProviderConfig:
        try:
            await self.verify_credentials(
                provider=config.provider,
                api_key=self.cipher.decrypt(config.encrypted_api_key),
                base_url=config.base_url,
            )
        except Exception as exc:
            config.last_error = str(exc)[:2000]
            await session.commit()
            raise
        config.last_verified_at = datetime.now(UTC)
        config.last_error = None
        await session.commit()
        await session.refresh(config)
        return config

    async def enabled_provider_names(self, session: AsyncSession) -> set[str]:
        return set(
            await session.scalars(select(ProviderConfig.provider).where(ProviderConfig.enabled.is_(True)))
        )

    async def runtime_connection(
        self,
        session: AsyncSession,
        provider: str,
    ) -> RuntimeProviderConnection:
        config = await session.scalar(
            select(ProviderConfig).where(
                ProviderConfig.provider == provider,
                ProviderConfig.enabled.is_(True),
            )
        )
        if config is None:
            raise ProviderUnavailable(
                f"No enabled {provider} provider connection is configured. Add one in management settings and retry."
            )
        runtime = config.runtime_options
        return RuntimeProviderConnection(
            provider_config_id=config.id,
            provider=config.provider,
            api_key=self.cipher.decrypt(config.encrypted_api_key),
            base_url=config.base_url,
            models=dict(config.models),
            timeout_seconds=float(runtime.get("timeout_seconds", 600)),
            max_retries=int(runtime.get("max_retries", 2)),
        )

    async def resolve_understand_options(
        self,
        session: AsyncSession,
        options: dict[str, Any],
    ) -> dict[str, Any]:
        return self._resolve_understand_options(
            await self._enabled_provider_configs(session),
            options,
        )

    async def understand_discovery_defaults(
        self,
        session: AsyncSession,
    ) -> tuple[dict[str, Any], set[str]]:
        configs = await self._enabled_provider_configs(session)
        return self._resolve_understand_options(configs, {}), set(configs)

    @staticmethod
    async def _enabled_provider_configs(session: AsyncSession) -> dict[str, ProviderConfig]:
        return {
            config.provider: config
            for config in await session.scalars(select(ProviderConfig).where(ProviderConfig.enabled.is_(True)))
        }

    @staticmethod
    def _resolve_understand_options(
        configs: dict[str, ProviderConfig],
        options: dict[str, Any],
    ) -> dict[str, Any]:
        if not configs:
            raise ProviderUnavailable(
                "No AI provider is configured. Add an OpenAI or xAI connection in management settings and retry."
            )
        default = next((config for config in configs.values() if config.is_default), None)
        default = default or configs.get("xai") or next(iter(configs.values()))
        resolved = dict(options)
        for stage in ("transcription", "planning", "vision", "summary"):
            provider_key = f"{stage}_provider"
            model_key = f"{stage}_model"
            provider = str(resolved.get(provider_key) or default.provider)
            config = configs.get(provider)
            if config is None:
                raise ProviderUnavailable(
                    f"The requested {provider} provider is not configured or is disabled."
                )
            resolved.setdefault(provider_key, provider)
            resolved.setdefault(model_key, config.models[stage])
        return resolved

    async def import_environment(self, session: AsyncSession) -> None:
        existing_configs = list(await session.scalars(select(ProviderConfig)))
        for config in existing_configs:
            self.cipher.decrypt(config.encrypted_api_key)
        existing = {config.provider for config in existing_configs}
        imports: list[tuple[str, str]] = []
        if self.settings.openai_api_key and "openai" not in existing:
            imports.append(("openai", self.settings.openai_api_key))
        if self.settings.xai_api_key and "xai" not in existing:
            imports.append(("xai", self.settings.xai_api_key))
        for provider, api_key in imports:
            await self.create(
                session,
                name=f"{provider} environment import",
                provider=provider,
                api_key=api_key,
                base_url=PROVIDER_BASE_URLS[provider],
                models=PROVIDER_MODELS[provider],
                timeout_seconds=(
                    self.settings.xai_timeout_seconds if provider == "xai" else self.settings.openai_timeout_seconds
                ),
                max_retries=(self.settings.xai_max_retries if provider == "xai" else self.settings.openai_max_retries),
                enabled=True,
                is_default=provider == "xai",
                verify=False,
            )

    @staticmethod
    async def verify_credentials(*, provider: str, api_key: str, base_url: str) -> None:
        client = AsyncOpenAI(api_key=api_key, base_url=f"{base_url.rstrip('/')}/", timeout=30, max_retries=0)
        try:
            await client.models.list()
        except Exception as exc:
            raise ProviderVerificationFailed(f"{provider} credential verification failed: {exc}") from exc
        finally:
            await client.close()

    @staticmethod
    async def _clear_default(session: AsyncSession) -> None:
        await session.execute(
            update(ProviderConfig)
            .where(ProviderConfig.is_default.is_(True))
            .values(is_default=False)
        )

    @staticmethod
    async def _has_default(session: AsyncSession) -> bool:
        return bool(
            await session.scalar(
                select(ProviderConfig.id).where(
                    ProviderConfig.enabled.is_(True),
                    ProviderConfig.is_default.is_(True),
                )
            )
        )
