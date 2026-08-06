from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import UploadFile

from .config import get_settings
from .models import (
    CallbackPayload,
    CodecPreference,
    EncodingQuality,
    JobDetail,
    JobRequest,
    JobResponse,
    JobStatus,
    QualityTarget,
)
from .transcode.engine import TranscodeCancelled, TranscodeEngine, TranscodeResult
from .utils.callbacks import CallbackDispatcher

logger = logging.getLogger(__name__)


class QueueFullError(RuntimeError):
    pass


class UploadTooLargeError(RuntimeError):
    pass


@dataclass
class JobRecord:
    job_id: str
    status: JobStatus
    created_at: datetime
    updated_at: datetime
    source_path: Path
    source_filename: str
    output_path: Path | None
    quality: QualityTarget
    codec: CodecPreference
    quality_profile: EncodingQuality
    callback_url: str | None
    error: str | None = None
    download_started_at: datetime | None = None
    download_finished_at: datetime | None = None
    transcode_started_at: datetime | None = None
    transcode_finished_at: datetime | None = None
    media_duration_seconds: float | None = None
    transcode_media_seconds: float | None = None
    source_width: int | None = None
    source_height: int | None = None
    cancel_requested: bool = False

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
            quality_profile=self.quality_profile,
            callback_url=self.callback_url,
            error=self.error,
            media_duration_seconds=self.media_duration_seconds,
            download_seconds=download_seconds,
            transcode_seconds=transcode_seconds,
            transcode_progress=progress,
            transcode_eta_seconds=eta_seconds,
            source_width=self.source_width,
            source_height=self.source_height,
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
        self.records: dict[str, JobRecord] = {}
        self.worker_task: asyncio.Task | None = None
        self.maintenance_task: asyncio.Task | None = None
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
        if self.queue.full():
            await upload.close()
            raise QueueFullError("Job queue is full")

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
            quality_profile=request.quality_profile,
            callback_url=str(request.callback_url) if request.callback_url else None,
            download_started_at=download_started,
            download_finished_at=download_finished,
        )
        self.records[job_id] = record

        try:
            self.queue.put_nowait(WorkItem(record=record, request=request))
        except asyncio.QueueFull as exc:
            self.records.pop(job_id, None)
            with contextlib.suppress(FileNotFoundError):
                dest_path.unlink()
            raise QueueFullError("Job queue is full") from exc
        queue_depth = self.queue.qsize()
        logger.info("Job queued", extra={"job_id": job_id, "queue_depth": queue_depth})

        return JobResponse(job_id=job_id, status=JobStatus.queued, message="Job accepted")

    async def get_job(self, job_id: str) -> JobDetail | None:
        record = self.records.get(job_id)
        return record.to_detail() if record else None

    async def list_jobs(self) -> list[JobDetail]:
        return [record.to_detail() for record in self.records.values()]

    async def cancel_job(self, job_id: str) -> bool:
        record = self.records.get(job_id)
        if not record:
            return False
        if record.status in {JobStatus.completed, JobStatus.failed, JobStatus.cancelled}:
            return False
        record.cancel_requested = True
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
            logger.info(
                "Processing job",
                extra={"job_id": record.job_id, "quality": record.quality.value, "codec": record.codec.value},
            )

            try:
                record.transcode_started_at = datetime.utcnow()
                result: TranscodeResult = await self.transcoder.process(record, work_item.request)
                record.transcode_finished_at = datetime.utcnow()
                if record.cancel_requested or record.status == JobStatus.cancelled:
                    with contextlib.suppress(FileNotFoundError):
                        result.output_path.unlink()
                    record.status = JobStatus.cancelled
                    record.updated_at = datetime.utcnow()
                    logger.info("Job cancelled", extra={"job_id": record.job_id})
                    await self._fire_callback(record, message="cancelled")
                    continue
                record.output_path = result.output_path
                record.status = JobStatus.completed
                record.updated_at = datetime.utcnow()
                logger.info("Job completed", extra={"job_id": record.job_id, "output": str(result.output_path)})
                await self._fire_callback(record, message="completed")
            except TranscodeCancelled:
                logger.info("Job cancelled", extra={"job_id": record.job_id})
                record.status = JobStatus.cancelled
                record.updated_at = datetime.utcnow()
                if not record.transcode_finished_at and record.transcode_started_at:
                    record.transcode_finished_at = datetime.utcnow()
                await self._fire_callback(record, message="cancelled")
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

    async def _fire_callback(self, record: JobRecord, message: str | None) -> None:
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
        total_bytes = 0
        try:
            with dest_path.open("wb") as out_file:
                while True:
                    chunk = await upload.read(1024 * 1024)
                    if not chunk:
                        break
                    total_bytes += len(chunk)
                    if self.settings.max_upload_bytes is not None and total_bytes > self.settings.max_upload_bytes:
                        out_file.close()
                        with contextlib.suppress(FileNotFoundError):
                            dest_path.unlink()
                        raise UploadTooLargeError(
                            f"Upload exceeds MEDIA_ENGINE_MAX_UPLOAD_BYTES={self.settings.max_upload_bytes}"
                        )
                    out_file.write(chunk)
        finally:
            await upload.close()
