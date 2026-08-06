from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .api.admin import router as admin_router
from .api.dashboard import router as dashboard_router
from .api.internal import router as internal_worker_router
from .api.observability import router as observability_router
from .api.v2 import router as v2_router
from .config import Settings, ensure_runtime_directories, get_settings, validate_control_plane_configuration
from .jobs import JobManager, QueueFullError, UploadTooLargeError
from .models import CodecPreference, EncodingQuality, JobDetail, JobListResponse, JobRequest, JobResponse, QualityTarget
from .persistence.database import check_database, close_database, configure_database, get_session_factory
from .security import CredentialCipher
from .selftest import SelfTestFailure, run_self_tests
from .services.providers import ProviderConfigurationService
from .services.worker_access import WorkerAccessService
from .storage.s3 import S3Store
from .transcode.engine import TranscodeEngine
from .utils.callbacks import CallbackDispatcher

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    ensure_runtime_directories(settings)
    app.state.settings = settings
    app.state.callbacks = CallbackDispatcher()
    app.state.transcoder = TranscodeEngine()
    app.state.job_manager = JobManager(app.state.transcoder, app.state.callbacks)

    if settings.platform_v2_enabled:
        validate_control_plane_configuration(settings)
        if not settings.database_url:
            raise RuntimeError("Platform v2 is enabled without MEDIA_ENGINE_DATABASE_URL")
        if settings.credential_encryption_key is None:
            raise RuntimeError("MEDIA_ENGINE_CREDENTIAL_ENCRYPTION_KEY is required")
        app.state.credential_cipher = CredentialCipher(settings.credential_encryption_key.get_secret_value())
        configure_database(settings.database_url)
        await check_database()
        async with get_session_factory()() as session:
            await ProviderConfigurationService(
                settings,
                app.state.credential_cipher,
            ).import_environment(session)
            if settings.initial_worker_token:
                await WorkerAccessService().import_initial(
                    session,
                    worker_key=settings.initial_worker_key,
                    display_name=settings.initial_worker_display_name,
                    profile=settings.initial_worker_profile,
                    token=settings.initial_worker_token,
                )
        app.state.s3_store = S3Store(settings)
        await app.state.s3_store.check_bucket()
        logger.info("Platform v2 PostgreSQL and S3 dependencies are ready")
    else:
        app.state.credential_cipher = None
        app.state.s3_store = None

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
        await close_database()


app = FastAPI(
    title="Media Engine",
    version=__version__,
    description=(
        "Product-neutral, S3-backed media processing with durable pipeline graphs. "
        "The `/v2` surface is the pre-stable public platform contract; `/jobs` is temporary legacy migration support."
    ),
    openapi_tags=[
        {
            "name": "platform-v2",
            "description": "Public asset, pipeline, job, artifact, and manifest contracts.",
        },
        {
            "name": "internal-workers",
            "description": "Bearer-authenticated worker control protocol; not a public client API.",
        },
        {
            "name": "management",
            "description": "Administrator-only provider, client, API-key, webhook, and usage management contracts.",
        },
    ],
    lifespan=lifespan,
)
app.include_router(v2_router)
app.include_router(admin_router)
app.include_router(observability_router)
app.include_router(internal_worker_router)
app.include_router(dashboard_router)
app.mount(
    "/admin/assets",
    StaticFiles(directory=Path(__file__).resolve().parent / "dashboard"),
    name="admin-assets",
)


@app.middleware("http")
async def administrator_security_headers(request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/admin") or request.url.path.startswith("/v2/admin"):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; base-uri 'none'; connect-src 'self'; "
            "font-src 'self'; form-action 'self'; frame-ancestors 'none'; "
            "img-src 'self' data:; object-src 'none'; script-src 'self'; style-src 'self'"
        )
    return response


def get_job_manager() -> JobManager:
    return app.state.job_manager


def get_callbacks() -> CallbackDispatcher:
    return app.state.callbacks


def get_transcoder() -> TranscodeEngine:
    return app.state.transcoder


@app.get("/healthz")
async def healthz(settings: Settings = Depends(get_settings)) -> dict[str, str]:
    return {"status": "ok", "app": settings.app_name}


@app.get("/readyz")
async def readyz(settings: Settings = Depends(get_settings)) -> dict[str, str]:
    platform_state = "ready" if settings.platform_v2_enabled and app.state.s3_store is not None else "disabled"
    return {"status": "ready", "app": settings.app_name, "platform_v2": platform_state}


@app.post("/jobs", response_model=JobResponse)
async def submit_job(
    file: UploadFile = File(..., description="Video file to transcode"),
    quality: QualityTarget = Form(QualityTarget.auto),
    codec: CodecPreference = Form(CodecPreference.auto),
    quality_profile: EncodingQuality = Form(EncodingQuality.balanced),
    callback_url: str | None = Form(None),
    job_manager: JobManager = Depends(get_job_manager),
) -> JobResponse:
    job_request = JobRequest(
        quality=quality,
        codec=codec,
        quality_profile=quality_profile,
        callback_url=callback_url,
    )
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
async def cancel_job(job_id: str, job_manager: JobManager = Depends(get_job_manager)) -> dict[str, str]:
    success = await job_manager.cancel_job(job_id)
    if not success:
        raise HTTPException(status_code=404, detail="Unable to cancel job")
    return {"status": "cancelled", "job_id": job_id}
