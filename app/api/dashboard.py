from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from ..config import Settings, get_settings
from ..security import ADMIN_SESSION_COOKIE
from .auth import get_admin_session_manager, require_admin, validate_admin_credentials

router = APIRouter(include_in_schema=False)
dashboard_root = Path(__file__).resolve().parent.parent / "dashboard"


class AdminLoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=1, max_length=4096)


class AdminSessionResponse(BaseModel):
    username: str


@router.get("/admin", response_class=FileResponse)
@router.get("/admin/", response_class=FileResponse)
async def dashboard() -> FileResponse:
    return FileResponse(
        dashboard_root / "index.html",
        headers={"Cache-Control": "no-store"},
    )


@router.post("/admin/session", status_code=status.HTTP_204_NO_CONTENT)
async def create_admin_session(
    payload: AdminLoginRequest,
    request: Request,
    settings: Settings = Depends(get_settings),
) -> Response:
    if not validate_admin_credentials(payload.username, payload.password, settings):
        raise HTTPException(status_code=401, detail="Invalid administrator credentials")
    manager = get_admin_session_manager(settings)
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    response.set_cookie(
        key=ADMIN_SESSION_COOKIE,
        value=manager.create(payload.username),
        max_age=settings.admin_session_ttl_hours * 3600,
        path="/",
        secure=settings.admin_session_cookie_secure or request.url.scheme == "https",
        httponly=True,
        samesite="strict",
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/admin/session", response_model=AdminSessionResponse)
async def get_admin_session(
    username: Annotated[str, Depends(require_admin)],
) -> AdminSessionResponse:
    return AdminSessionResponse(username=username)


@router.delete("/admin/session", status_code=status.HTTP_204_NO_CONTENT)
async def delete_admin_session(
    request: Request,
    _: Annotated[str, Depends(require_admin)],
    settings: Settings = Depends(get_settings),
) -> Response:
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    response.delete_cookie(
        key=ADMIN_SESSION_COOKIE,
        path="/",
        secure=settings.admin_session_cookie_secure or request.url.scheme == "https",
        httponly=True,
        samesite="strict",
    )
    response.headers["Cache-Control"] = "no-store"
    return response
