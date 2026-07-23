from __future__ import annotations

import secrets
from collections.abc import Callable
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBasic, HTTPBasicCredentials, HTTPBearer

from ..config import Settings, get_settings
from ..persistence.database import get_session_factory
from ..security import ADMIN_SESSION_COOKIE, AdminSessionManager, CredentialCipher
from ..services.access import AccessService, ClientPrincipal

admin_basic = HTTPBasic(auto_error=False)
client_bearer = HTTPBearer(auto_error=False)
ADMIN_UI_REQUEST_HEADER = "X-Media-Engine-Admin-UI"


def validate_admin_credentials(username: str, password: str, settings: Settings) -> bool:
    expected_username = settings.admin_username or ""
    expected_password = settings.admin_password.get_secret_value() if settings.admin_password else ""
    return secrets.compare_digest(username.encode(), expected_username.encode()) and secrets.compare_digest(
        password.encode(), expected_password.encode()
    )


def get_admin_session_manager(settings: Settings) -> AdminSessionManager:
    if settings.admin_session_secret is None or settings.admin_password is None:
        raise HTTPException(status_code=503, detail="Administrator browser sessions are not configured")
    return AdminSessionManager(
        session_secret=settings.admin_session_secret.get_secret_value(),
        admin_password=settings.admin_password.get_secret_value(),
        ttl_hours=settings.admin_session_ttl_hours,
    )


def require_admin(
    request: Request,
    credentials: Annotated[HTTPBasicCredentials | None, Depends(admin_basic)],
    settings: Settings = Depends(get_settings),
) -> str:
    username = settings.admin_username or ""
    if credentials is not None and validate_admin_credentials(
        credentials.username,
        credentials.password,
        settings,
    ):
        return username

    token = request.cookies.get(ADMIN_SESSION_COOKIE)
    if token:
        session_username = get_admin_session_manager(settings).verify(
            token,
            expected_username=username,
        )
        if session_username is not None:
            if (
                request.method not in {"GET", "HEAD", "OPTIONS"}
                and request.headers.get(ADMIN_UI_REQUEST_HEADER) != "1"
            ):
                raise HTTPException(status_code=403, detail="Administrator UI request header is required")
            return session_username

    headers = (
        {}
        if request.headers.get(ADMIN_UI_REQUEST_HEADER) == "1"
        else {"WWW-Authenticate": "Basic"}
    )
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid administrator credentials",
        headers=headers,
    )


def get_credential_cipher(request: Request) -> CredentialCipher:
    cipher = getattr(request.app.state, "credential_cipher", None)
    if cipher is None:
        raise HTTPException(status_code=503, detail="Credential encryption is not ready")
    return cipher


def require_client_scope(scope: str) -> Callable:
    async def dependency(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(client_bearer)],
    ) -> ClientPrincipal:
        if credentials is None or credentials.scheme.lower() != "bearer":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="A Media Engine API key is required",
                headers={"WWW-Authenticate": "Bearer"},
            )
        factory = get_session_factory()
        async with factory() as session:
            principal = await AccessService().authenticate(session, credentials.credentials)
        if principal is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid, expired, or revoked API key",
                headers={"WWW-Authenticate": "Bearer"},
            )
        try:
            principal.require(scope)
        except PermissionError as exc:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
        return principal

    return dependency
