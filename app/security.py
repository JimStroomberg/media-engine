from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from cryptography.fernet import Fernet, InvalidToken


class CredentialDecryptionError(RuntimeError):
    pass


class CredentialCipher:
    """Authenticated encryption for secrets persisted by the control plane."""

    def __init__(self, key: str) -> None:
        try:
            self._fernet = Fernet(key.encode("ascii"))
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "MEDIA_ENGINE_CREDENTIAL_ENCRYPTION_KEY must be a URL-safe base64 Fernet key"
            ) from exc

    def encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode("utf-8")).decode("ascii")

    def decrypt(self, value: str) -> str:
        try:
            return self._fernet.decrypt(value.encode("ascii")).decode("utf-8")
        except InvalidToken as exc:
            raise CredentialDecryptionError(
                "Stored credentials cannot be decrypted with the configured encryption key"
            ) from exc


@dataclass(frozen=True)
class GeneratedApiKey:
    token: str
    prefix: str
    token_hash: str


@dataclass(frozen=True)
class GeneratedWorkerToken:
    token: str
    prefix: str
    token_hash: str


def generate_api_key() -> GeneratedApiKey:
    prefix = secrets.token_hex(6)
    secret = secrets.token_urlsafe(32)
    token = f"me_{prefix}_{secret}"
    return GeneratedApiKey(token=token, prefix=prefix, token_hash=hash_api_key(token))


def generate_worker_token() -> GeneratedWorkerToken:
    """Return a one-time worker credential suitable for hashed persistence."""

    prefix = secrets.token_hex(6)
    secret = secrets.token_urlsafe(32)
    token = f"mew_{prefix}_{secret}"
    return GeneratedWorkerToken(token=token, prefix=prefix, token_hash=hash_api_key(token))


def generate_webhook_signing_secret() -> str:
    """Return a high-entropy endpoint secret that is displayed only once."""

    return f"whsec_{secrets.token_urlsafe(32)}"


def hash_api_key(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def credential_hint(value: str) -> str:
    if len(value) <= 8:
        return "configured"
    return f"{value[:4]}...{value[-4:]}"


ADMIN_SESSION_COOKIE = "media_engine_admin_session"


class AdminSessionManager:
    """Issue stateless, password-bound administrator browser sessions."""

    def __init__(self, *, session_secret: str, admin_password: str, ttl_hours: int) -> None:
        if len(session_secret) < 32:
            raise ValueError("MEDIA_ENGINE_ADMIN_SESSION_SECRET must contain at least 32 characters")
        self.ttl = timedelta(hours=ttl_hours)
        self._signing_key = hmac.new(
            session_secret.encode("utf-8"),
            admin_password.encode("utf-8"),
            hashlib.sha256,
        ).digest()

    def create(self, username: str, *, now: datetime | None = None) -> str:
        issued_at = now or datetime.now(UTC)
        payload = {
            "username": username,
            "expires_at": int((issued_at + self.ttl).timestamp()),
            "nonce": secrets.token_urlsafe(18),
        }
        encoded = self._encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
        signature = self._encode(hmac.new(self._signing_key, encoded.encode("ascii"), hashlib.sha256).digest())
        return f"{encoded}.{signature}"

    def verify(
        self,
        token: str,
        *,
        expected_username: str,
        now: datetime | None = None,
    ) -> str | None:
        try:
            encoded, supplied_signature = token.split(".", 1)
            expected_signature = self._encode(
                hmac.new(self._signing_key, encoded.encode("ascii"), hashlib.sha256).digest()
            )
            if not secrets.compare_digest(supplied_signature, expected_signature):
                return None
            payload = json.loads(self._decode(encoded))
            username = payload["username"]
            expires_at = int(payload["expires_at"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None
        current = now or datetime.now(UTC)
        if username != expected_username or expires_at <= int(current.timestamp()):
            return None
        return username

    @staticmethod
    def _encode(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")

    @staticmethod
    def _decode(value: str) -> bytes:
        padding = "=" * (-len(value) % 4)
        return base64.urlsafe_b64decode(value + padding)
