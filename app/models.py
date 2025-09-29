from __future__ import annotations

from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import List, Optional

from pydantic import BaseModel, Field, HttpUrl


class JobStatus(str, Enum):
    queued = "queued"
    processing = "processing"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"


class QualityTarget(str, Enum):
    auto = "auto"
    uhd_2160p = "uhd_2160p"
    fhd_1080p = "fhd_1080p"
    hd_720p = "hd_720p"
    sd_480p = "sd_480p"


class CodecPreference(str, Enum):
    auto = "auto"
    h264 = "h264"
    h265 = "h265"


class JobRequest(BaseModel):
    quality: QualityTarget = Field(QualityTarget.auto, description="Desired output quality preset")
    codec: CodecPreference = Field(CodecPreference.auto, description="Preferred codec for output")
    callback_url: Optional[HttpUrl] = Field(None, description="Optional webhook to call when the job completes")


class JobResponse(BaseModel):
    job_id: str
    status: JobStatus
    message: Optional[str] = None


class JobDetail(BaseModel):
    job_id: str
    status: JobStatus
    created_at: datetime
    updated_at: datetime
    source_filename: str
    output_filename: Optional[str] = None
    output_path: Optional[Path] = None
    quality: QualityTarget
    codec: CodecPreference
    callback_url: Optional[HttpUrl]
    error: Optional[str] = None
    media_duration_seconds: Optional[float] = None
    download_seconds: Optional[float] = None
    transcode_seconds: Optional[float] = None
    transcode_progress: Optional[float] = None
    transcode_eta_seconds: Optional[float] = None

    class Config:
        json_encoders = {
            Path: lambda p: str(p),
            datetime: lambda d: d.isoformat(),
        }


class JobListResponse(BaseModel):
    jobs: List[JobDetail]


class CallbackPayload(BaseModel):
    job_id: str
    status: JobStatus
    output_path: Optional[str] = None
    message: Optional[str] = None
