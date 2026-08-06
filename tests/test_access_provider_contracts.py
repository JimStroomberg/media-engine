from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.config import Settings, get_settings, validate_control_plane_configuration
from app.main import app
from app.processors.usage import capture_provider_usage, mark_usage_outcome, record_provider_usage
from app.security import AdminSessionManager, CredentialCipher, generate_api_key, generate_worker_token, hash_api_key


def test_control_plane_credentials_are_required_without_a_global_worker_token() -> None:
    with pytest.raises(RuntimeError, match="MEDIA_ENGINE_ADMIN_USERNAME"):
        validate_control_plane_configuration(Settings(_env_file=None))

    validate_control_plane_configuration(
        Settings(
            _env_file=None,
            admin_username="admin",
            admin_password="password",
            admin_session_secret="s" * 48,
            credential_encryption_key=Fernet.generate_key().decode("ascii"),
        )
    )


def test_provider_credentials_use_authenticated_encryption() -> None:
    cipher = CredentialCipher(Fernet.generate_key().decode("ascii"))
    plaintext = "provider-secret"

    ciphertext = cipher.encrypt(plaintext)

    assert plaintext not in ciphertext
    assert cipher.decrypt(ciphertext) == plaintext


def test_generated_api_keys_are_high_entropy_and_hashable() -> None:
    generated = generate_api_key()

    assert generated.token.startswith(f"me_{generated.prefix}_")
    assert generated.token_hash == hash_api_key(generated.token)
    assert len(generated.token_hash) == 64


def test_generated_worker_tokens_are_separate_high_entropy_credentials() -> None:
    generated = generate_worker_token()

    assert generated.token.startswith(f"mew_{generated.prefix}_")
    assert generated.token_hash == hash_api_key(generated.token)
    assert len(generated.token_hash) == 64


def test_admin_sessions_are_signed_password_bound_and_expire() -> None:
    now = datetime.now(UTC)
    manager = AdminSessionManager(
        session_secret="s" * 48,
        admin_password="admin-password",
        ttl_hours=12,
    )
    token = manager.create("admin", now=now)

    assert manager.verify(token, expected_username="admin", now=now) == "admin"
    assert manager.verify(token, expected_username="someone-else", now=now) is None
    assert manager.verify(token, expected_username="admin", now=now + timedelta(hours=13)) is None
    assert AdminSessionManager(
        session_secret="s" * 48,
        admin_password="changed-password",
        ttl_hours=12,
    ).verify(token, expected_username="admin", now=now) is None


def test_dashboard_session_cookie_and_csrf_header_contract() -> None:
    settings = Settings(
        _env_file=None,
        admin_username="admin",
        admin_password="admin-password",
        admin_session_secret="s" * 48,
    )
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        client = TestClient(app)
        dashboard = client.get("/admin")
        assert dashboard.status_code == 200
        assert "Media Engine" in dashboard.text
        assert "frame-ancestors 'none'" in dashboard.headers["content-security-policy"]

        login = client.post(
            "/admin/session",
            json={"username": "admin", "password": "admin-password"},
        )
        assert login.status_code == 204
        assert "HttpOnly" in login.headers["set-cookie"]
        assert "SameSite=strict" in login.headers["set-cookie"]
        assert client.get("/admin/session").json() == {"username": "admin"}
        assert client.delete("/admin/session").status_code == 403
        assert client.delete(
            "/admin/session",
            headers={"X-Media-Engine-Admin-UI": "1"},
        ).status_code == 204
    finally:
        app.dependency_overrides.clear()


def test_dashboard_auth_failure_does_not_trigger_browser_basic_dialog() -> None:
    settings = Settings(
        _env_file=None,
        admin_username="admin",
        admin_password="admin-password",
        admin_session_secret="s" * 48,
    )
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        client = TestClient(app)

        dashboard_session = client.get(
            "/admin/session",
            headers={"X-Media-Engine-Admin-UI": "1"},
        )
        assert dashboard_session.status_code == 401
        assert "www-authenticate" not in dashboard_session.headers

        direct_api = client.get("/v2/admin/overview")
        assert direct_api.status_code == 401
        assert direct_api.headers["www-authenticate"] == "Basic"
    finally:
        app.dependency_overrides.clear()


def test_management_provider_schema_never_returns_credentials() -> None:
    properties = app.openapi()["components"]["schemas"]["ProviderResponse"]["properties"]

    assert "api_key" not in properties
    assert "encrypted_api_key" not in properties
    assert "credential_hint" in properties


def test_management_observability_contract_is_in_openapi() -> None:
    paths = app.openapi()["paths"]

    assert "/v2/admin/overview" in paths
    assert "/v2/admin/workers" in paths
    assert "post" in paths["/v2/admin/workers"]
    assert "/v2/admin/workers/{worker_id}/drain" in paths
    assert "/v2/admin/workers/{worker_id}/activate" in paths
    assert "/v2/admin/workers/{worker_id}/revoke" in paths
    assert "/v2/admin/workers/{worker_id}/rotate-token" in paths
    assert "delete" in paths["/v2/admin/workers/{worker_id}"]
    assert "/v2/internal/stages/{stage_id}/artifacts/prepare-upload" in paths
    assert "/v2/admin/jobs" in paths
    assert "/v2/admin/jobs/{job_id}" in paths


def test_provider_usage_is_captured_before_later_stage_outcome() -> None:
    started_at = datetime.now(UTC) - timedelta(milliseconds=25)
    with capture_provider_usage() as events:
        record_provider_usage(
            provider="xai",
            model="grok-4.5",
            operation="summary",
            usage={"cost_in_usd_ticks": 123},
            started_at=started_at,
        )

    mark_usage_outcome(events, "stage_failed")

    assert len(events) == 1
    assert events[0].usage == {"cost_in_usd_ticks": 123}
    assert events[0].outcome == "stage_failed"
    assert events[0].latency_ms >= 0
