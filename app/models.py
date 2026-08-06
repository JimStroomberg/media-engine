from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, HttpUrl


class JobStatus(StrEnum):
    queued = "queued"
    processing = "processing"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"


class QualityTarget(StrEnum):
    auto = "auto"
    uhd_2160p = "uhd_2160p"
    qhd_1440p = "qhd_1440p"
    fhd_1080p = "fhd_1080p"
    hd_720p = "hd_720p"
    sd_480p = "sd_480p"
    low_360p = "low_360p"
    audio_only = "audio_only"


class CodecPreference(StrEnum):
    auto = "auto"
    h264 = "h264"
    h265 = "h265"


class EncodingQuality(StrEnum):
    compact = "compact"
    balanced = "balanced"
    high = "high"


class JobRequest(BaseModel):
    quality: QualityTarget = Field(
        QualityTarget.auto,
        description="Desired output quality preset (use audio_only for AAC extraction)",
    )
    codec: CodecPreference = Field(CodecPreference.auto, description="Preferred codec for output")
    quality_profile: EncodingQuality = Field(
        EncodingQuality.balanced,
        description="Compression profile controlling the bitrate-quality envelope",
    )
    callback_url: HttpUrl | None = Field(None, description="Optional webhook to call when the job completes")


class JobResponse(BaseModel):
    job_id: str
    status: JobStatus
    message: str | None = None


class JobDetail(BaseModel):
    job_id: str
    status: JobStatus
    created_at: datetime
    updated_at: datetime
    source_filename: str
    output_filename: str | None = None
    output_path: Path | None = None
    quality: QualityTarget
    codec: CodecPreference
    quality_profile: EncodingQuality
    callback_url: HttpUrl | None
    error: str | None = None
    media_duration_seconds: float | None = None
    download_seconds: float | None = None
    transcode_seconds: float | None = None
    transcode_progress: float | None = None
    transcode_eta_seconds: float | None = None
    source_width: int | None = None
    source_height: int | None = None

    model_config = ConfigDict()


class JobListResponse(BaseModel):
    jobs: list[JobDetail]


class CallbackPayload(BaseModel):
    job_id: str
    status: JobStatus
    output_path: str | None = None
    message: str | None = None
