from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Optional, Union

from pydantic import BaseSettings, Field, validator


class Settings(BaseSettings):
    """Application runtime configuration."""

    app_name: str = Field("media-engine", description="Service identifier")
    api_host: str = Field("0.0.0.0", description="Interface for the API server")
    api_port: int = Field(8080, description="Port for the API server")

    data_root: Path = Field(Path("/data"), description="Base directory for stored assets")
    input_dir: Path = Field(Path("/data/input"), description="Directory for uploaded assets")
    work_dir: Path = Field(Path("/data/work"), description="Scratch workspace for transcodes")
    output_dir: Path = Field(Path("/data/output"), description="Directory containing finished outputs")
    temp_dir: Path = Field(Path("/tmp/media-engine"), description="Temporary directory for probes")

    max_queue_size: int = Field(50, description="Maximum number of queued jobs")
    job_retention_minutes: int = Field(120, description="How long to keep completed job metadata")

    callback_timeout_seconds: int = Field(10, description="HTTP timeout for webhook callbacks")
    callback_max_attempts: int = Field(3, description="Retries for webhook callbacks")

    self_test_on_startup: bool = Field(True, description="Run self-test pipeline when the app boots")

    logfile_path: Optional[Path] = Field(None, description="Optional path for structured JSON logs")

    ffmpeg_command: str = Field("ffmpeg", description="Executable used for media transcoding")
    ffprobe_command: str = Field("ffprobe", description="Executable used for media probing")

    require_rk_accel: bool = Field(False, description="Fail startup when RKMPP hardware acceleration is expected but missing")

    class Config:
        env_prefix = "MEDIA_ENGINE_"
        env_file = ".env"
        env_file_encoding = "utf-8"

    @validator("input_dir", "work_dir", "output_dir", pre=True)
    def _expand_path(cls, value: Union[Path, str]) -> Path:
        return Path(value).expanduser()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached Settings instance."""

    settings = Settings()
    settings.input_dir.mkdir(parents=True, exist_ok=True)
    settings.work_dir.mkdir(parents=True, exist_ok=True)
    settings.output_dir.mkdir(parents=True, exist_ok=True)
    settings.temp_dir.mkdir(parents=True, exist_ok=True)
    return settings
