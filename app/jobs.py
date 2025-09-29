from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Optional, List

from fastapi import UploadFile

from .config import get_settings
from .models import (
    CallbackPayload,
    CodecPreference,
    JobDetail,
    JobRequest,
    JobResponse,
    JobStatus,
    QualityTarget,
)
from .transcode.engine import TranscodeEngine, TranscodeResult
from .utils.callbacks import CallbackDispatcher

logger = logging.getLogger(__name__)


@dataclass
class JobRecord:
    job_id: str
    status: JobStatus
    created_at: datetime
    updated_at: datetime
    source_path: Path
    source_filename: str
    output_path: Optional[Path]
    quality: QualityTarget
    codec: CodecPreference
    callback_url: Optional[str]
    error: Optional[str] = None
    download_started_at: Optional[datetime] = None
    download_finished_at: Optional[datetime] = None
    transcode_started_at: Optional[datetime] = None
    transcode_finished_at: Optional[datetime] = None
    media_duration_seconds: Optional[float] = None
    transcode_media_seconds: Optional[float] = None

    def to_detail(self) -> JobDetail:
        now = datetime.utcnow()
        download_seconds = None
        if self.download_started_at and self.download_finished_at:
            download_seconds = (self.download_finished_at - self.download_started_at).total_seconds()

        transcode_seconds = None
        if self.transcode_started_at:
            end_time = self.transcode_finished_at or now
            transcode_seconds = (end_time - self.transcode_started_at).total_seconds()

        progress = None
        eta_seconds = None
        if self.media_duration_seconds and self.transcode_media_seconds is not None and self.transcode_started_at:
            media_duration = self.media_duration_seconds
            processed = min(self.transcode_media_seconds, media_duration)
            progress = processed / media_duration if media_duration else None
            if progress and progress > 0 and self.transcode_finished_at is None:
                elapsed = (now - self.transcode_started_at).total_seconds()
                if elapsed > 0:
                    speed = processed / elapsed
                    remaining_media = max(media_duration - processed, 0.0)
                    if speed > 0:
                        eta_seconds = remaining_media / speed

        return JobDetail(
            job_id=self.job_id,
            status=self.status,
            created_at=self.created_at,
            updated_at=self.updated_at,
            source_filename=self.source_filename,
            output_filename=self.output_path.name if self.output_path else None,
            output_path=self.output_path,
            quality=self.quality,
            codec=self.codec,
            callback_url=self.callback_url,
            error=self.error,
            media_duration_seconds=self.media_duration_seconds,
            download_seconds=download_seconds,
            transcode_seconds=transcode_seconds,
            transcode_progress=progress,
            transcode_eta_seconds=eta_seconds,
        )


@dataclass
class WorkItem:
    record: JobRecord
    request: JobRequest


class JobManager:
    """Cooperative job queue ensuring only one active transcode."""

    def __init__(self, transcoder: TranscodeEngine, callbacks: CallbackDispatcher) -> None:
        self.settings = get_settings()
        self.transcoder = transcoder
        self.callbacks = callbacks
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=self.settings.max_queue_size)
        self.records: Dict[str, JobRecord] = {}
        self.worker_task: Optional[asyncio.Task] = None
        self.maintenance_task: Optional[asyncio.Task] = None
        self._shutdown = asyncio.Event()

    async def start(self) -> None:
        if self.worker_task is None:
            self.worker_task = asyncio.create_task(self._worker(), name="transcode-worker")
            logger.info("Job manager worker started")
        if self.maintenance_task is None:
            self.maintenance_task = asyncio.create_task(self._maintenance_loop(), name="transcode-maintenance")

    async def stop(self) -> None:
        self._shutdown.set()
        for task in (self.worker_task, self.maintenance_task):
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self.worker_task = None
        self.maintenance_task = None

    async def submit_job(self, upload: UploadFile, request: JobRequest) -> JobResponse:
        job_id = str(uuid.uuid4())
        timestamp = datetime.utcnow()
        original_name = Path(upload.filename or "upload").name
        dest_path = self.settings.input_dir / f"{job_id}_{original_name}"

        logger.info("Saving uploaded file", extra={"job_id": job_id, "source_file": original_name})
        download_started = datetime.utcnow()
        await self._persist_upload(upload, dest_path)
        download_finished = datetime.utcnow()
        file_size = dest_path.stat().st_size
        logger.info(
            "Upload persisted",
            extra={"job_id": job_id, "source_file": original_name, "file_bytes": file_size},
        )

        record = JobRecord(
            job_id=job_id,
            status=JobStatus.queued,
            created_at=timestamp,
            updated_at=timestamp,
            source_path=dest_path,
            source_filename=original_name,
            output_path=None,
            quality=request.quality,
            codec=request.codec,
            callback_url=str(request.callback_url) if request.callback_url else None,
            download_started_at=download_started,
            download_finished_at=download_finished,
        )
        self.records[job_id] = record

        await self.queue.put(WorkItem(record=record, request=request))
        queue_depth = self.queue.qsize()
        logger.info("Job queued", extra={"job_id": job_id, "queue_depth": queue_depth})

        return JobResponse(job_id=job_id, status=JobStatus.queued, message="Job accepted")

    async def get_job(self, job_id: str) -> Optional[JobDetail]:
        record = self.records.get(job_id)
        return record.to_detail() if record else None

    async def list_jobs(self) -> List[JobDetail]:
        return [record.to_detail() for record in self.records.values()]

    async def cancel_job(self, job_id: str) -> bool:
        record = self.records.get(job_id)
        if not record:
            return False
        if record.status in {JobStatus.completed, JobStatus.failed, JobStatus.cancelled}:
            return False
        record.status = JobStatus.cancelled
        record.updated_at = datetime.utcnow()
        return True

    async def purge_expired_jobs(self) -> None:
        cutoff = datetime.utcnow() - timedelta(minutes=self.settings.job_retention_minutes)
        to_delete = [job_id for job_id, record in self.records.items() if record.updated_at < cutoff]
        for job_id in to_delete:
            record = self.records.pop(job_id)
            with contextlib.suppress(FileNotFoundError):
                record.source_path.unlink()
            if record.output_path:
                with contextlib.suppress(FileNotFoundError):
                    record.output_path.unlink()
            logger.debug("Purged job", extra={"job_id": job_id})

    async def _maintenance_loop(self) -> None:
        try:
            while not self._shutdown.is_set():
                await self.purge_expired_jobs()
                await asyncio.sleep(60)
        except asyncio.CancelledError:
            logger.debug("Maintenance loop cancelled")
            raise

    async def _worker(self) -> None:
        while not self._shutdown.is_set():
            try:
                work_item: WorkItem = await self.queue.get()
            except asyncio.CancelledError:
                break

            record = work_item.record
            logger.info("Dequeued job", extra={"job_id": record.job_id, "queue_depth": self.queue.qsize()})
            if record.status == JobStatus.cancelled:
                logger.info("Skipping cancelled job", extra={"job_id": record.job_id})
                self.queue.task_done()
                continue

            record.status = JobStatus.processing
            record.updated_at = datetime.utcnow()
            logger.info("Processing job", extra={"job_id": record.job_id, "quality": record.quality.value, "codec": record.codec.value})

            try:
                record.transcode_started_at = datetime.utcnow()
                result: TranscodeResult = await self.transcoder.process(record, work_item.request)
                record.transcode_finished_at = datetime.utcnow()
                record.output_path = result.output_path
                record.status = JobStatus.completed
                record.updated_at = datetime.utcnow()
                logger.info("Job completed", extra={"job_id": record.job_id, "output": str(result.output_path)})
                await self._fire_callback(record, message="completed")
            except Exception as exc:  # noqa: BLE001
                logger.exception("Job failed", extra={"job_id": record.job_id})
                record.status = JobStatus.failed
                record.updated_at = datetime.utcnow()
                record.error = str(exc)
                if not record.transcode_finished_at and record.transcode_started_at:
                    record.transcode_finished_at = datetime.utcnow()
                await self._fire_callback(record, message=str(exc))
            finally:
                self.queue.task_done()

    async def _fire_callback(self, record: JobRecord, message: Optional[str]) -> None:
        if not record.callback_url:
            return
        payload = CallbackPayload(
            job_id=record.job_id,
            status=record.status,
            output_path=str(record.output_path) if record.output_path else None,
            message=message,
        )
        await self.callbacks.dispatch(record.callback_url, payload)

    async def _persist_upload(self, upload: UploadFile, dest_path: Path) -> None:
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        with dest_path.open("wb") as out_file:
            while True:
                chunk = await upload.read(1024 * 1024)
                if not chunk:
                    break
                out_file.write(chunk)
        await upload.close()
