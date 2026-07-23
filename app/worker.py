from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import shutil
import signal
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from .config import Settings, ensure_runtime_directories, get_settings
from .domain.content import content_addressed_key, sha256_file
from .models import CodecPreference, JobRequest, JobStatus, QualityTarget
from .processors import LocalMediaProcessor, OpenAIMediaProcessor, ProducedArtifact, StageInputFile, XAIMediaProcessor
from .processors.usage import ProviderUsageEvent, capture_provider_usage, mark_usage_outcome
from .selftest import run_self_tests
from .storage.s3 import S3Store
from .transcode.engine import TranscodeCancelled, TranscodeEngine

logger = logging.getLogger(__name__)


class WorkerLeaseLost(RuntimeError):
    pass


@dataclass
class WorkerStageRecord:
    job_id: str
    source_path: Path
    quality: QualityTarget = QualityTarget.auto
    codec: CodecPreference = CodecPreference.auto
    status: JobStatus = JobStatus.processing
    source_width: int | None = None
    source_height: int | None = None
    media_duration_seconds: float | None = None
    transcode_media_seconds: float | None = None
    progress: float | None = 0.0
    cancel_requested: bool = False
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class MediaWorker:
    def __init__(self, settings: Settings) -> None:
        if not settings.worker_api_token:
            raise RuntimeError("MEDIA_ENGINE_WORKER_API_TOKEN is required")
        if not settings.s3_bucket:
            raise RuntimeError("MEDIA_ENGINE_S3_BUCKET is required")
        self.settings = settings
        self.store = S3Store(settings)
        self.engine = TranscodeEngine()
        self.local_processor = LocalMediaProcessor(
            ffmpeg_command=settings.ffmpeg_command,
            ffprobe_command=settings.ffprobe_command,
            tesseract_command=settings.tesseract_command,
            timeout=settings.media_command_timeout_seconds,
        )
        self.worker_id: str | None = None
        self._shutdown = asyncio.Event()

    async def run(self) -> None:
        headers = {"Authorization": f"Bearer {self.settings.worker_api_token}"}
        timeout = httpx.Timeout(30.0, connect=10.0)
        async with httpx.AsyncClient(base_url=self.settings.worker_api_url, headers=headers, timeout=timeout) as client:
            await self._register_until_ready(client)
            while not self._shutdown.is_set():
                try:
                    claim = await self._claim(client)
                    if claim is None:
                        await self._wait_for_poll()
                        continue
                    await self._process_claim(client, claim)
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001
                    logger.exception("Worker loop iteration failed")
                    await self._wait_for_poll()
        logger.info("Worker stopped")

    def request_shutdown(self) -> None:
        if not self._shutdown.is_set():
            logger.info("Worker shutdown requested; active work may finish during the container grace period")
            self._shutdown.set()

    async def _wait_for_poll(self) -> None:
        try:
            await asyncio.wait_for(
                self._shutdown.wait(),
                timeout=self.settings.worker_poll_seconds,
            )
        except TimeoutError:
            pass

    async def _register_until_ready(self, client: httpx.AsyncClient) -> None:
        processors = []
        if shutil.which(self.settings.ffmpeg_command) and shutil.which(self.settings.ffprobe_command):
            processors.append("ffmpeg")
        if shutil.which(self.settings.tesseract_command):
            processors.append("tesseract")
        payload = {
            "worker_key": self.settings.worker_key,
            "display_name": self.settings.worker_display_name,
            "capabilities": {
                "pipelines": ["transcode"],
                "backends": [self.settings.worker_backend],
                "encoders": ["h264", "h265"],
                "processors": processors,
                "providers": ["openai", "xai"],
            },
        }
        while not self._shutdown.is_set():
            try:
                await self.store.check_bucket()
                response = await client.post("/v2/internal/workers/register", json=payload)
                response.raise_for_status()
                self.worker_id = response.json()["worker_id"]
                logger.info(
                    "Worker registered and S3 access verified",
                    extra={"worker_id": self.worker_id, "bucket": self.store.bucket},
                )
                return
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("Worker API or S3 readiness check failed; retrying")
                await self._wait_for_poll()

    async def _claim(self, client: httpx.AsyncClient) -> dict[str, Any] | None:
        if self.worker_id is None:
            raise RuntimeError("Worker is not registered")
        response = await client.post("/v2/internal/stages/claim", json={"worker_id": self.worker_id})
        if response.status_code == 204:
            return None
        if response.status_code == 404:
            await self._register_until_ready(client)
            return None
        response.raise_for_status()
        claim: dict[str, Any] = response.json()
        logger.info(
            "Stage claimed",
            extra={"stage_id": claim["stage_id"], "pipeline_run_id": claim["pipeline_run_id"]},
        )
        return claim

    async def _process_claim(self, client: httpx.AsyncClient, claim: dict[str, Any]) -> None:
        if self.worker_id is None:
            raise RuntimeError("Worker is not registered")

        stage_id = claim["stage_id"]
        lease_token = claim["lease_token"]
        stage_root = self.settings.temp_dir / stage_id
        input_dir = stage_root / "input"
        output_dir = stage_root / "output"
        input_dir.mkdir(parents=True, exist_ok=True)
        output_dir.mkdir(parents=True, exist_ok=True)
        record: WorkerStageRecord | None = None
        produced: list[ProducedArtifact] = []
        stop_heartbeat = asyncio.Event()
        lease_lost = asyncio.Event()
        heartbeat_task: asyncio.Task[None] | None = None
        provider_processor: OpenAIMediaProcessor | XAIMediaProcessor | None = None
        usage_events: list[ProviderUsageEvent] = []

        try:
            inputs = await self._download_inputs(claim, input_dir)
            source = inputs["source"]
            record = WorkerStageRecord(job_id=stage_id, source_path=source.path)
            heartbeat_task = asyncio.create_task(
                self._heartbeat_loop(
                    client,
                    stage_id=stage_id,
                    lease_token=lease_token,
                    record=record,
                    stop=stop_heartbeat,
                    lease_lost=lease_lost,
                ),
                name=f"stage-heartbeat-{stage_id}",
            )

            processor = claim["processor"]
            with capture_provider_usage() as usage_events:
                if processor == "transcode":
                    produced = [await self._transcode(record, claim, output_dir)]
                elif processor.startswith(("openai_", "ai_")):
                    provider_processor = self._create_provider_processor(claim)
                    produced = await provider_processor.process(
                        processor,
                        inputs=inputs,
                        options=claim["options"],
                        output_dir=output_dir,
                    )
                else:
                    produced = await self.local_processor.process(
                        processor,
                        inputs=inputs,
                        options=claim["options"],
                        output_dir=output_dir,
                    )
            record.progress = 0.9
            if lease_lost.is_set():
                raise WorkerLeaseLost("Lease was lost while processing")

            completion_artifacts = []
            output_contracts = {output["artifact_type"]: output for output in claim["outputs"]}
            for artifact in produced:
                contract = output_contracts.get(artifact.artifact_type)
                if contract is None:
                    raise RuntimeError(f"Processor emitted undeclared artifact type {artifact.artifact_type}")
                if artifact.schema_version != contract["schema_version"]:
                    raise RuntimeError(
                        f"Processor emitted {artifact.artifact_type} schema {artifact.schema_version}; "
                        f"expected {contract['schema_version']}"
                    )
                artifact_sha256, artifact_size = await asyncio.to_thread(sha256_file, artifact.path)
                object_key = content_addressed_key(artifact_sha256)
                stored = await self.store.upload_file(
                    artifact.path,
                    object_key,
                    sha256=artifact_sha256,
                    media_type=artifact.media_type,
                )
                completion_artifacts.append(
                    {
                        "sha256": artifact_sha256,
                        "size_bytes": artifact_size,
                        "media_type": artifact.media_type,
                        "object_key": stored.key,
                        "etag": stored.etag,
                        "artifact_type": artifact.artifact_type,
                        "format": artifact.format,
                        "schema_version": artifact.schema_version,
                    }
                )

            mark_usage_outcome(usage_events, "completed")
            response = await client.post(
                f"/v2/internal/stages/{stage_id}/complete",
                json={
                    "worker_id": self.worker_id,
                    "lease_token": lease_token,
                    "artifacts": completion_artifacts,
                    "usage_events": [event.as_payload() for event in usage_events],
                },
            )
            if response.status_code == 409:
                raise WorkerLeaseLost(response.text)
            response.raise_for_status()
            record.progress = 1.0
            logger.info(
                "Stage completed",
                extra={"stage_id": stage_id, "artifacts": [item.artifact_type for item in produced]},
            )
        except (WorkerLeaseLost, TranscodeCancelled):
            logger.warning("Stage stopped after lease loss", extra={"stage_id": stage_id})
        except Exception as exc:  # noqa: BLE001
            logger.exception("Stage processing failed", extra={"stage_id": stage_id})
            mark_usage_outcome(usage_events, "stage_failed")
            await self._report_failure(
                client,
                stage_id=stage_id,
                lease_token=lease_token,
                error=str(exc),
                usage_events=usage_events,
            )
        finally:
            stop_heartbeat.set()
            if record is not None:
                record.cancel_requested = True
            if heartbeat_task is not None:
                heartbeat_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat_task
            if provider_processor is not None:
                await provider_processor.close()
            for artifact in produced:
                artifact.path.unlink(missing_ok=True)
            shutil.rmtree(stage_root, ignore_errors=True)

    async def _download_inputs(self, claim: dict[str, Any], input_dir: Path) -> dict[str, StageInputFile]:
        source = claim["source"]
        source_format = self._format_for_media_type(source.get("media_type"))
        source_path = input_dir / f"source.{source_format}"
        await self._download_and_verify(source, source_path)
        inputs = {
            "source": StageInputFile(
                artifact_type="source",
                path=source_path,
                media_type=source.get("media_type"),
                format=source_format,
            )
        }
        for item in claim.get("inputs", []):
            artifact_type = str(item["artifact_type"])
            safe_name = re.sub(r"[^a-zA-Z0-9_.-]", "_", artifact_type)
            destination = input_dir / f"{safe_name}.{item['format']}"
            await self._download_and_verify(item, destination)
            inputs[artifact_type] = StageInputFile(
                artifact_type=artifact_type,
                path=destination,
                media_type=item.get("media_type"),
                format=item["format"],
                schema_version=item.get("schema_version"),
                producer_stage=item.get("producer_stage"),
            )
        return inputs

    async def _download_and_verify(self, item: dict[str, Any], destination: Path) -> None:
        await self.store.download_file(item["object_key"], destination)
        actual_sha256, actual_size = await asyncio.to_thread(sha256_file, destination)
        if actual_sha256 != item["sha256"] or actual_size != item["size_bytes"]:
            raise RuntimeError("Downloaded stage input failed SHA-256 or size verification")

    async def _transcode(
        self,
        record: WorkerStageRecord,
        claim: dict[str, Any],
        output_dir: Path,
    ) -> ProducedArtifact:
        options = claim["options"]
        request = JobRequest(
            quality=QualityTarget(options["quality"]),
            codec=CodecPreference(options["codec"]),
        )
        record.quality = request.quality
        record.codec = request.codec
        result = await self.engine.process(record, request)
        destination = output_dir / result.output_path.name
        shutil.move(result.output_path, destination)
        media_type = "audio/mp4" if destination.suffix == ".m4a" else "video/mp4"
        return ProducedArtifact(
            artifact_type="transcoded_media",
            path=destination,
            media_type=media_type,
            format=destination.suffix.lstrip("."),
        )

    async def _heartbeat_loop(
        self,
        client: httpx.AsyncClient,
        *,
        stage_id: str,
        lease_token: str,
        record: WorkerStageRecord,
        stop: asyncio.Event,
        lease_lost: asyncio.Event,
    ) -> None:
        if self.worker_id is None:
            raise RuntimeError("Worker is not registered")
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.settings.worker_heartbeat_seconds)
                return
            except TimeoutError:
                pass

            progress = record.progress
            if record.media_duration_seconds and record.transcode_media_seconds is not None:
                progress = min(record.transcode_media_seconds / record.media_duration_seconds, 1.0)
            try:
                response = await client.post(
                    f"/v2/internal/stages/{stage_id}/heartbeat",
                    json={
                        "worker_id": self.worker_id,
                        "lease_token": lease_token,
                        "progress": progress,
                    },
                )
                if response.status_code == 409:
                    lease_lost.set()
                    record.cancel_requested = True
                    return
                response.raise_for_status()
            except Exception:  # noqa: BLE001
                logger.exception("Stage heartbeat failed", extra={"stage_id": stage_id})

    async def _report_failure(
        self,
        client: httpx.AsyncClient,
        *,
        stage_id: str,
        lease_token: str,
        error: str,
        usage_events: list[ProviderUsageEvent],
    ) -> None:
        if self.worker_id is None:
            return
        try:
            response = await client.post(
                f"/v2/internal/stages/{stage_id}/fail",
                json={
                    "worker_id": self.worker_id,
                    "lease_token": lease_token,
                    "error_message": error[:4000] or "Worker failed without an error message",
                    "usage_events": [event.as_payload() for event in usage_events],
                },
            )
            if response.status_code != 409:
                response.raise_for_status()
        except Exception:  # noqa: BLE001
            logger.exception("Unable to report stage failure", extra={"stage_id": stage_id})

    def _create_provider_processor(
        self,
        claim: dict[str, Any],
    ) -> OpenAIMediaProcessor | XAIMediaProcessor:
        connection = claim.get("provider_connection")
        if not isinstance(connection, dict):
            raise RuntimeError("Provider-backed stage claim has no runtime provider connection")
        common = {
            "api_key": str(connection["api_key"]),
            "base_url": str(connection["base_url"]),
            "timeout_seconds": float(connection["timeout_seconds"]),
            "max_retries": int(connection["max_retries"]),
        }
        provider = str(connection["provider"])
        if provider == "openai":
            return OpenAIMediaProcessor(self.settings, **common)
        if provider == "xai":
            return XAIMediaProcessor(self.settings, **common)
        raise RuntimeError(f"Unsupported provider connection: {provider}")

    @staticmethod
    def _format_for_media_type(media_type: str | None) -> str:
        return {
            "video/mp4": "mp4",
            "audio/mp4": "m4a",
            "video/webm": "webm",
            "audio/webm": "webm",
            "audio/mpeg": "mp3",
            "image/gif": "gif",
        }.get(media_type or "", "media")


async def async_main() -> None:
    settings = get_settings()
    ensure_runtime_directories(settings)
    if settings.self_test_on_startup:
        await asyncio.to_thread(run_self_tests)
    worker = MediaWorker(settings)
    loop = asyncio.get_running_loop()
    with contextlib.suppress(NotImplementedError):
        loop.add_signal_handler(signal.SIGTERM, worker.request_shutdown)
    try:
        await worker.run()
    finally:
        with contextlib.suppress(NotImplementedError):
            loop.remove_signal_handler(signal.SIGTERM)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
