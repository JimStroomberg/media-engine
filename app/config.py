from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application runtime configuration."""

    model_config = SettingsConfigDict(
        env_prefix="MEDIA_ENGINE_",
        env_file=(".env", ".env.local"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = Field("media-engine", description="Service identifier")
    api_host: str = Field("0.0.0.0", description="Interface for the API server")
    api_port: int = Field(8080, description="Port for the API server")

    data_root: Path = Field(Path("/data"), description="Base directory for stored assets")
    input_dir: Path = Field(Path("/data/input"), description="Directory for uploaded assets")
    work_dir: Path = Field(Path("/data/work"), description="Scratch workspace for transcodes")
    output_dir: Path = Field(Path("/data/output"), description="Directory containing finished outputs")
    temp_dir: Path = Field(Path("/tmp/media-engine"), description="Temporary directory for probes")

    max_queue_size: int = Field(50, description="Maximum number of queued jobs")
    max_upload_bytes: int | None = Field(None, description="Reject uploads larger than this many bytes")
    job_retention_minutes: int = Field(120, description="How long to keep completed job metadata")

    callback_timeout_seconds: int = Field(10, description="HTTP timeout for webhook callbacks")
    callback_max_attempts: int = Field(3, description="Retries for webhook callbacks")

    self_test_on_startup: bool = Field(True, description="Run self-test pipeline when the app boots")
    allow_cpu_fallback: bool = Field(True, description="Permit CPU video transcode fallback when hardware fails")

    logfile_path: Path | None = Field(None, description="Optional path for structured JSON logs")

    ffmpeg_command: str = Field("ffmpeg", description="Executable used for media transcoding")
    ffprobe_command: str = Field("ffprobe", description="Executable used for media probing")
    ffprobe_timeout_seconds: int = Field(30, description="Maximum time to wait for ffprobe")
    media_command_timeout_seconds: int = Field(
        1800,
        ge=30,
        description="Maximum time for one worker media or OCR command",
    )
    tesseract_command: str = Field("tesseract", description="Executable used for local OCR")

    require_rk_accel: bool = Field(
        False,
        description="Fail startup when RKMPP hardware acceleration is expected but missing",
    )

    database_url: str | None = Field(None, description="Async SQLAlchemy URL for the platform PostgreSQL database")
    s3_endpoint_url: str | None = Field(None, description="Optional S3-compatible endpoint such as MinIO")
    s3_public_endpoint_url: str | None = Field(None, description="Client-reachable endpoint used for signed S3 URLs")
    s3_access_key_id: str | None = Field(
        None,
        description="S3 access key; omit when the SDK credential chain is used",
    )
    s3_secret_access_key: str | None = Field(
        None,
        description="S3 secret key; omit when the SDK credential chain is used",
    )
    s3_region: str = Field("us-east-1", description="S3 signing region")
    s3_bucket: str | None = Field(None, description="Bucket used for durable media-engine blobs")
    s3_force_path_style: bool = Field(True, description="Use path-style S3 addressing for MinIO compatibility")
    asset_retention_hours: int = Field(24, ge=1, description="Default retention for ingested assets")
    pipeline_retention_hours: int = Field(24, ge=1, description="Default retention for pipeline runs and artifacts")
    artifact_url_expiry_seconds: int = Field(900, ge=60, le=86400, description="Lifetime of signed artifact URLs")
    retention_interval_seconds: int = Field(60, ge=5, description="Delay between expired-blob sweeps")
    retention_batch_size: int = Field(100, ge=1, le=1000, description="Maximum blobs handled in one retention sweep")

    worker_api_token: str | None = Field(None, description="Bearer token accepted by internal worker endpoints")
    worker_api_url: str = Field("http://api:8080", description="Control-plane base URL used by worker processes")
    worker_key: str = Field("worker-cpu-1", description="Stable worker identity within a deployment")
    worker_display_name: str = Field("CPU worker", description="Human-readable worker name")
    worker_backend: str = Field("cpu", description="Worker hardware backend capability")
    transcode_required_backend: str | None = Field(
        None,
        description="Optional backend capability required for newly queued transcode stages",
    )
    worker_lease_seconds: int = Field(90, ge=15, description="Duration of a claimed stage lease")
    worker_heartbeat_seconds: int = Field(20, ge=5, description="Worker heartbeat interval during processing")
    worker_poll_seconds: float = Field(2.0, ge=0.2, description="Delay between empty stage claims")

    admin_username: str | None = Field(
        None,
        description="Required administrator login name for management APIs",
    )
    admin_password: SecretStr | None = Field(
        None,
        description="Required administrator password for management APIs",
    )
    admin_session_secret: SecretStr | None = Field(
        None,
        description="Stable high-entropy HMAC secret used for administrator browser sessions",
    )
    admin_session_ttl_hours: int = Field(12, ge=1, le=168)
    admin_session_cookie_secure: bool = Field(
        False,
        description="Always mark the administrator session cookie Secure; enable behind production HTTPS",
    )
    credential_encryption_key: SecretStr | None = Field(
        None,
        description="Stable Fernet key used to encrypt provider credentials stored in PostgreSQL",
    )

    webhook_dispatch_interval_seconds: float = Field(1.0, ge=0.1, le=60.0)
    webhook_timeout_seconds: float = Field(10.0, ge=1.0, le=120.0)
    webhook_max_attempts: int = Field(8, ge=1, le=100)
    webhook_initial_backoff_seconds: int = Field(10, ge=1, le=3600)
    webhook_max_backoff_seconds: int = Field(3600, ge=1, le=86400)
    webhook_lease_seconds: int = Field(60, ge=5, le=600)
    webhook_response_preview_bytes: int = Field(1000, ge=0, le=16384)
    webhook_allow_private_addresses: bool = Field(
        False,
        description="Allow HTTP and private webhook destinations for isolated local development only",
    )

    openai_api_key: str | None = Field(
        None,
        validation_alias=AliasChoices("OPENAI_API_KEY", "MEDIA_ENGINE_OPENAI_API_KEY"),
        description="OpenAI project API key used only by workers with OpenAI capabilities",
    )
    openai_timeout_seconds: float = Field(600.0, ge=10.0, le=3600.0)
    openai_max_retries: int = Field(2, ge=0, le=10)

    xai_api_key: str | None = Field(
        None,
        validation_alias=AliasChoices("XAI_API_KEY", "MEDIA_ENGINE_XAI_API_KEY"),
        description="xAI project API key used only by workers with xAI capabilities",
    )
    xai_base_url: str = Field("https://api.x.ai/v1", description="xAI API base URL")
    xai_timeout_seconds: float = Field(600.0, ge=10.0, le=3600.0)
    xai_max_retries: int = Field(2, ge=0, le=10)

    @field_validator("input_dir", "work_dir", "output_dir", mode="before")
    def _expand_path(cls, value: Path | str) -> Path:
        return Path(value).expanduser()

    @property
    def platform_v2_enabled(self) -> bool:
        """Return whether the PostgreSQL and S3 platform foundation is configured."""

        return bool(self.database_url and self.s3_bucket)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached Settings instance."""

    return Settings()


def ensure_runtime_directories(settings: Settings) -> None:
    """Create process-local directories only when an application process starts."""

    settings.input_dir.mkdir(parents=True, exist_ok=True)
    settings.work_dir.mkdir(parents=True, exist_ok=True)
    settings.output_dir.mkdir(parents=True, exist_ok=True)
    settings.temp_dir.mkdir(parents=True, exist_ok=True)


def validate_control_plane_configuration(settings: Settings) -> None:
    """Fail early with actionable names when required control-plane secrets are absent."""

    required = {
        "MEDIA_ENGINE_ADMIN_USERNAME": settings.admin_username,
        "MEDIA_ENGINE_ADMIN_PASSWORD": settings.admin_password,
        "MEDIA_ENGINE_ADMIN_SESSION_SECRET": settings.admin_session_secret,
        "MEDIA_ENGINE_CREDENTIAL_ENCRYPTION_KEY": settings.credential_encryption_key,
        "MEDIA_ENGINE_WORKER_API_TOKEN": settings.worker_api_token,
    }
    missing = []
    for name, value in required.items():
        if isinstance(value, SecretStr):
            value = value.get_secret_value()
        if value is None or (isinstance(value, str) and not value.strip()):
            missing.append(name)
    if missing:
        names = ", ".join(missing)
        raise RuntimeError(f"Missing required control-plane credentials: {names}. Add them and retry startup.")
