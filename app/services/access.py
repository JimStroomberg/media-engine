from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from ..persistence.models import ApiKey, ClientProject
from ..security import GeneratedApiKey, generate_api_key, hash_api_key

ALL_CLIENT_SCOPES = frozenset({"assets:read", "assets:write", "jobs:read", "jobs:write"})


class ClientConflict(RuntimeError):
    pass


class ApiKeyConflict(RuntimeError):
    pass


@dataclass(frozen=True)
class ClientPrincipal:
    client_id: uuid.UUID
    api_key_id: uuid.UUID
    scopes: frozenset[str]

    def require(self, scope: str) -> None:
        if scope not in self.scopes:
            raise PermissionError(f"API key does not grant {scope}")


class AccessService:
    async def create_client(self, session: AsyncSession, *, name: str, enabled: bool = True) -> ClientProject:
        if await session.scalar(select(ClientProject.id).where(ClientProject.name == name)):
            raise ClientConflict(f"Client {name!r} already exists")
        client = ClientProject(id=uuid.uuid4(), name=name, enabled=enabled)
        session.add(client)
        await session.commit()
        await session.refresh(client)
        return client

    async def list_clients(self, session: AsyncSession) -> list[ClientProject]:
        return list(await session.scalars(select(ClientProject).order_by(ClientProject.name)))

    async def create_api_key(
        self,
        session: AsyncSession,
        *,
        client: ClientProject,
        name: str,
        scopes: list[str],
        expires_at: datetime | None,
    ) -> tuple[ApiKey, GeneratedApiKey]:
        unknown = set(scopes) - ALL_CLIENT_SCOPES
        if unknown:
            raise ValueError(f"Unknown API key scopes: {', '.join(sorted(unknown))}")
        if await session.scalar(
            select(ApiKey.id).where(ApiKey.client_id == client.id, ApiKey.name == name)
        ):
            raise ApiKeyConflict(f"API key {name!r} already exists for this client")
        generated = generate_api_key()
        api_key = ApiKey(
            id=uuid.uuid4(),
            client_id=client.id,
            name=name,
            key_prefix=generated.prefix,
            key_hash=generated.token_hash,
            scopes=sorted(set(scopes)),
            expires_at=expires_at,
        )
        session.add(api_key)
        await session.commit()
        await session.refresh(api_key)
        return api_key, generated

    async def list_api_keys(self, session: AsyncSession, client_id: uuid.UUID) -> list[ApiKey]:
        return list(
            await session.scalars(
                select(ApiKey).where(ApiKey.client_id == client_id).order_by(ApiKey.created_at.desc())
            )
        )

    async def revoke_api_key(self, session: AsyncSession, api_key: ApiKey) -> ApiKey:
        api_key.revoked_at = api_key.revoked_at or datetime.now(UTC)
        await session.commit()
        await session.refresh(api_key)
        return api_key

    async def authenticate(self, session: AsyncSession, token: str) -> ClientPrincipal | None:
        parts = token.split("_", 2)
        if len(parts) != 3 or parts[0] != "me":
            return None
        api_key = await session.scalar(
            select(ApiKey)
            .where(ApiKey.key_prefix == parts[1])
            .options(joinedload(ApiKey.client))
        )
        now = datetime.now(UTC)
        if (
            api_key is None
            or api_key.revoked_at is not None
            or (api_key.expires_at is not None and api_key.expires_at <= now)
            or not api_key.client.enabled
            or not secrets.compare_digest(api_key.key_hash, hash_api_key(token))
        ):
            return None
        api_key.last_used_at = now
        await session.commit()
        return ClientPrincipal(
            client_id=api_key.client_id,
            api_key_id=api_key.id,
            scopes=frozenset(api_key.scopes),
        )
