from __future__ import annotations

from contextlib import asynccontextmanager
import logging
from typing import Dict, Optional

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

from .config import Settings, get_settings
from .jobs import JobManager, QueueFullError, UploadTooLargeError
from .models import CodecPreference, JobDetail, JobListResponse, JobRequest, JobResponse, QualityTarget
from .selftest import SelfTestFailure, run_self_tests
from .transcode.engine import TranscodeEngine
from .utils.callbacks import CallbackDispatcher

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    app.state.settings = settings
    app.state.callbacks = CallbackDispatcher()
    app.state.transcoder = TranscodeEngine()
    app.state.job_manager = JobManager(app.state.transcoder, app.state.callbacks)

    if settings.self_test_on_startup:
        try:
            run_self_tests()
            logger.info("Self-tests passed")
        except SelfTestFailure as exc:
            logger.error("Self-test failure: %s", exc)
            raise

    await app.state.job_manager.start()
    try:
        yield
    finally:
        job_manager: JobManager = app.state.job_manager
        callbacks: CallbackDispatcher = app.state.callbacks
        await job_manager.stop()
        await callbacks.shutdown()


app = FastAPI(title="Media Engine", version="0.1.0", lifespan=lifespan)


def get_job_manager() -> JobManager:
    return app.state.job_manager


def get_callbacks() -> CallbackDispatcher:
    return app.state.callbacks


def get_transcoder() -> TranscodeEngine:
    return app.state.transcoder


@app.get("/healthz")
async def healthz(settings: Settings = Depends(get_settings)) -> Dict[str, str]:
    return {"status": "ok", "app": settings.app_name}


@app.post("/jobs", response_model=JobResponse)
async def submit_job(
    file: UploadFile = File(..., description="Video file to transcode"),
    quality: QualityTarget = Form(QualityTarget.auto),
    codec: CodecPreference = Form(CodecPreference.auto),
    callback_url: Optional[str] = Form(None),
    job_manager: JobManager = Depends(get_job_manager),
) -> JobResponse:
    job_request = JobRequest(quality=quality, codec=codec, callback_url=callback_url)
    try:
        return await job_manager.submit_job(file, job_request)
    except QueueFullError as exc:
        raise HTTPException(status_code=503, detail=str(exc), headers={"Retry-After": "30"}) from exc
    except UploadTooLargeError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc


@app.get("/jobs", response_model=JobListResponse)
async def list_jobs(job_manager: JobManager = Depends(get_job_manager)) -> JobListResponse:
    jobs = await job_manager.list_jobs()
    return JobListResponse(jobs=jobs)


@app.get("/jobs/{job_id}", response_model=JobDetail)
async def get_job(job_id: str, job_manager: JobManager = Depends(get_job_manager)) -> JobDetail:
    job = await job_manager.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@app.get("/jobs/{job_id}/download")
async def download_job(job_id: str, job_manager: JobManager = Depends(get_job_manager)) -> FileResponse:
    job = await job_manager.get_job(job_id)
    if not job or not job.output_path:
        raise HTTPException(status_code=404, detail="Job output not ready")
    media_type = "audio/mp4" if job.output_path.suffix == ".m4a" else "video/mp4"
    return FileResponse(path=job.output_path, filename=job.output_path.name, media_type=media_type)


@app.delete("/jobs/{job_id}")
async def cancel_job(job_id: str, job_manager: JobManager = Depends(get_job_manager)) -> Dict[str, str]:
    success = await job_manager.cancel_job(job_id)
    if not success:
        raise HTTPException(status_code=404, detail="Unable to cancel job")
    return {"status": "cancelled", "job_id": job_id}
