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

import aiofiles
import httpx

from . import __version__
from .config import Settings, ensure_runtime_directories, get_settings
from .domain.content import sha256_file
from .hardware import detected_worker_capabilities, detected_worker_runtime
from .models import CodecPreference, EncodingQuality, JobRequest, JobStatus, QualityTarget
from .processors import LocalMediaProcessor, OpenAIMediaProcessor, ProducedArtifact, StageInputFile, XAIMediaProcessor
from .processors.usage import ProviderUsageEvent, capture_provider_usage, mark_usage_outcome
from .selftest import run_self_tests
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
    quality_profile: EncodingQuality = EncodingQuality.balanced
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
        worker_token = settings.resolved_worker_token()
        if not worker_token:
            raise RuntimeError("MEDIA_ENGINE_WORKER_TOKEN or MEDIA_ENGINE_WORKER_TOKEN_FILE is required")
        self.settings = settings
        self.worker_token = worker_token
        self.engine = TranscodeEngine()
        self.capabilities = detected_worker_capabilities(settings)
        self.local_processor = LocalMediaProcessor(
            ffmpeg_command=settings.ffmpeg_command,
            ffprobe_command=settings.ffprobe_command,
            tesseract_command=settings.tesseract_command,
            timeout=settings.media_command_timeout_seconds,
        )
        self.worker_id: str | None = None
        self._shutdown = asyncio.Event()

    async def run(self) -> None:
        headers = {"Authorization": f"Bearer {self.worker_token}"}
        timeout = httpx.Timeout(30.0, connect=10.0)
        transfer_timeout = httpx.Timeout(None, connect=30.0)
        async with (
            httpx.AsyncClient(base_url=self.settings.worker_api_url, headers=headers, timeout=timeout) as client,
            httpx.AsyncClient(timeout=transfer_timeout, follow_redirects=True) as transfer_client,
        ):
            await self._register_until_ready(client)
            while not self._shutdown.is_set():
                try:
                    claim = await self._claim(client)
                    if claim is None:
                        await self._wait_for_poll()
                        continue
                    await self._process_claim(client, transfer_client, claim)
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
        payload = {
            "capabilities": self.capabilities,
            "runtime": detected_worker_runtime(self.settings, version=__version__),
        }
        while not self._shutdown.is_set():
            try:
                response = await client.post("/v2/internal/workers/register", json=payload)
                response.raise_for_status()
                self.worker_id = response.json()["worker_id"]
                logger.info(
                    "Worker authenticated and registered",
                    extra={"worker_id": self.worker_id},
                )
                return
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("Worker registration failed; retrying")
                await self._wait_for_poll()

    async def _claim(self, client: httpx.AsyncClient) -> dict[str, Any] | None:
        if self.worker_id is None:
            raise RuntimeError("Worker is not registered")
        response = await client.post("/v2/internal/stages/claim")
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

    async def _process_claim(
        self,
        client: httpx.AsyncClient,
        transfer_client: httpx.AsyncClient,
        claim: dict[str, Any],
    ) -> None:
        if self.worker_id is None:
            raise RuntimeError("Worker is not registered")

        stage_id = claim["stage_id"]
        lease_token = claim["lease_token"]
        stage_root = self.settings.temp_dir / stage_id
        input_dir = stage_root / "input"
        output_dir = stage_root / "output"
        input_dir.mkdir(parents=True, exist_ok=True)
        output_dir.mkdir(parents=True, exist_ok=True)
        record = WorkerStageRecord(job_id=stage_id, source_path=input_dir / "source.media")
        produced: list[ProducedArtifact] = []
        stop_heartbeat = asyncio.Event()
        lease_lost = asyncio.Event()
        heartbeat_task: asyncio.Task[None] | None = None
        provider_processor: OpenAIMediaProcessor | XAIMediaProcessor | None = None
        usage_events: list[ProviderUsageEvent] = []

        try:
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
            inputs = await self._download_inputs(transfer_client, claim, input_dir)
            source = inputs["source"]
            record.source_path = source.path

            processor = claim["processor"]
            if processor == "transcode":
                unsupported_codec = await self._unsupported_input_codec(source.path, claim["options"])
                if unsupported_codec is not None:
                    stop_heartbeat.set()
                    await self._decline_claim(
                        client,
                        stage_id=stage_id,
                        lease_token=lease_token,
                        input_codec=unsupported_codec,
                    )
                    return
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
                prepare_response = await client.post(
                    f"/v2/internal/stages/{stage_id}/artifacts/prepare-upload",
                    json={
                        "lease_token": lease_token,
                        "sha256": artifact_sha256,
                        "size_bytes": artifact_size,
                        "media_type": artifact.media_type,
                        "artifact_type": artifact.artifact_type,
                        "format": artifact.format,
                        "schema_version": artifact.schema_version,
                    },
                )
                if prepare_response.status_code == 409:
                    raise WorkerLeaseLost(prepare_response.text)
                prepare_response.raise_for_status()
                prepared = prepare_response.json()
                if prepared["upload_required"]:
                    upload_url = prepared.get("upload_url")
                    if not upload_url:
                        raise RuntimeError("Control plane requested an upload without providing a URL")
                    await self._upload_file(
                        transfer_client,
                        upload_url,
                        prepared.get("headers", {}),
                        artifact.path,
                    )
                completion_artifacts.append(
                    {
                        "sha256": artifact_sha256,
                        "size_bytes": artifact_size,
                        "media_type": artifact.media_type,
                        "artifact_type": artifact.artifact_type,
                        "format": artifact.format,
                        "schema_version": artifact.schema_version,
                    }
                )

            mark_usage_outcome(usage_events, "completed")
            response = await client.post(
                f"/v2/internal/stages/{stage_id}/complete",
                json={
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

    async def _download_inputs(
        self,
        transfer_client: httpx.AsyncClient,
        claim: dict[str, Any],
        input_dir: Path,
    ) -> dict[str, StageInputFile]:
        source = claim["source"]
        source_format = self._format_for_media_type(source.get("media_type"))
        source_path = input_dir / f"source.{source_format}"
        await self._download_and_verify(transfer_client, source, source_path)
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
            await self._download_and_verify(transfer_client, item, destination)
            inputs[artifact_type] = StageInputFile(
                artifact_type=artifact_type,
                path=destination,
                media_type=item.get("media_type"),
                format=item["format"],
                schema_version=item.get("schema_version"),
                producer_stage=item.get("producer_stage"),
            )
        return inputs

    async def _download_and_verify(
        self,
        transfer_client: httpx.AsyncClient,
        item: dict[str, Any],
        destination: Path,
    ) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            async with transfer_client.stream("GET", item["download_url"]) as response:
                response.raise_for_status()
                async with aiofiles.open(destination, "wb") as output:
                    async for chunk in response.aiter_bytes(1024 * 1024):
                        await output.write(chunk)
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(f"Signed stage download failed with HTTP {exc.response.status_code}") from None
        except httpx.HTTPError:
            raise RuntimeError("Signed stage download request failed") from None
        actual_sha256, actual_size = await asyncio.to_thread(sha256_file, destination)
        if actual_sha256 != item["sha256"] or actual_size != item["size_bytes"]:
            raise RuntimeError("Downloaded stage input failed SHA-256 or size verification")

    @staticmethod
    async def _upload_file(
        transfer_client: httpx.AsyncClient,
        upload_url: str,
        headers: dict[str, str],
        source: Path,
    ) -> None:
        async def chunks():
            async with aiofiles.open(source, "rb") as input_file:
                while chunk := await input_file.read(1024 * 1024):
                    yield chunk

        try:
            response = await transfer_client.put(upload_url, headers=headers, content=chunks())
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(f"Signed artifact upload failed with HTTP {exc.response.status_code}") from None
        except httpx.HTTPError:
            raise RuntimeError("Signed artifact upload request failed") from None

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
            quality_profile=EncodingQuality(options.get("quality_profile", EncodingQuality.balanced.value)),
        )
        record.quality = request.quality
        record.codec = request.codec
        record.quality_profile = request.quality_profile
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

    async def _unsupported_input_codec(self, source_path: Path, options: dict[str, Any]) -> str | None:
        if options.get("quality") == QualityTarget.audio_only.value:
            return None
        if self.settings.worker_backend not in {"rkmpp", "nvv4l2"} or self.settings.allow_cpu_fallback:
            return None
        input_codec = await asyncio.to_thread(self.engine.input_codec, source_path)
        if input_codec is None:
            return None
        supported_decoders = set(self.capabilities.get("decoders", []))
        return input_codec if input_codec not in supported_decoders else None

    @staticmethod
    async def _decline_claim(
        client: httpx.AsyncClient,
        *,
        stage_id: str,
        lease_token: str,
        input_codec: str,
    ) -> None:
        response = await client.post(
            f"/v2/internal/stages/{stage_id}/decline",
            json={
                "lease_token": lease_token,
                "reason_code": "unsupported_input_codec",
                "input_codec": input_codec,
            },
        )
        if response.status_code == 409:
            raise WorkerLeaseLost(response.text)
        response.raise_for_status()
        logger.info(
            "Stage lease declined without consuming an attempt",
            extra={"stage_id": stage_id, "reason_code": "unsupported_input_codec", "input_codec": input_codec},
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
        try:
            response = await client.post(
                f"/v2/internal/stages/{stage_id}/fail",
                json={
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
    # Request logs include complete presigned S3 URLs, which are temporary credentials.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
