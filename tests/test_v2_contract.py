from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.api.v2 import JobCreateRequest, parse_source_metadata
from app.config import Settings
from app.domain.pipelines import (
    UnsupportedPipeline,
    get_pipeline,
    list_pipelines,
    required_capabilities_for_stage,
)
from app.persistence.models import Base, Blob
from app.services.assets import AssetService, StagedUpload
from app.storage.s3 import StoredObject


def test_source_metadata_accepts_object() -> None:
    assert parse_source_metadata('{"provider":"youtube","source_id":"abc"}') == {
        "provider": "youtube",
        "source_id": "abc",
    }


@pytest.mark.parametrize("raw", ["[]", '"value"', "not-json"])
def test_source_metadata_rejects_non_object(raw: str) -> None:
    with pytest.raises(HTTPException) as raised:
        parse_source_metadata(raw)
    assert raised.value.status_code == 422


def test_platform_requires_database_and_bucket() -> None:
    assert not Settings(database_url=None, s3_bucket="media").platform_v2_enabled
    assert not Settings(database_url="postgresql+asyncpg://db/media", s3_bucket=None).platform_v2_enabled
    assert Settings(database_url="postgresql+asyncpg://db/media", s3_bucket="media").platform_v2_enabled


def test_foundation_tables_are_registered() -> None:
    assert set(Base.metadata.tables) == {
        "ai_usage_events",
        "api_keys",
        "artifacts",
        "assets",
        "blobs",
        "clients",
        "job_requests",
        "pipeline_runs",
        "provider_configs",
        "stage_dependencies",
        "stage_runs",
        "workers",
        "webhook_delivery_attempts",
        "webhook_endpoints",
        "webhook_events",
    }
    assert "source_blob_id" in Base.metadata.tables["pipeline_runs"].columns
    assert "asset_id" in Base.metadata.tables["job_requests"].columns
    assert "run_reused" in Base.metadata.tables["job_requests"].columns
    assert "producer_stage_run_id" in Base.metadata.tables["artifacts"].columns
    assert "webhook_endpoint_id" in Base.metadata.tables["job_requests"].columns


def test_transcode_pipeline_is_server_versioned() -> None:
    pipeline = get_pipeline("transcode")

    assert pipeline.version == "1"
    assert pipeline.required_artifacts == ("transcoded_media",)
    assert [item.name for item in list_pipelines()] == ["ai_prepare", "transcode", "understand"]


def test_ai_prepare_pipeline_declares_a_durable_stage_graph() -> None:
    pipeline = get_pipeline("ai_prepare")

    assert [stage.name for stage in pipeline.stages] == [
        "probe",
        "audio_extract",
        "subtitle_extract",
        "scene_detect",
        "keyframe_extract",
        "ocr",
    ]
    assert pipeline.stage("audio_extract").depends_on == ("probe",)
    assert pipeline.stage("keyframe_extract").depends_on == ("scene_detect",)
    assert pipeline.stage("ocr").depends_on == ("keyframe_extract",)
    assert pipeline.stage("audio_extract").required_artifact_types == {"audio_metadata"}
    assert "audio" not in pipeline.required_artifacts


def test_understand_pipeline_uses_provider_neutral_stage_contracts() -> None:
    pipeline = get_pipeline("understand")
    options = pipeline.normalize_options({})

    assert pipeline.version == "2"
    assert pipeline.schema_version == "2"
    assert options["transcription_provider"] == "xai"
    assert options["vision_provider"] == "xai"
    assert options["summary_provider"] == "xai"
    assert options["planning_provider"] == "xai"
    assert options["analysis_profile"] == "auto"
    assert options["coarse_max_keyframes"] == 12
    assert options["targeted_keyframes"] == 12
    assert options["max_keyframes"] == 24
    assert pipeline.stage("transcribe").required_capabilities == {"providers": ("openai",)}
    assert pipeline.stage("content_plan").depends_on == (
        "probe",
        "subtitle_extract",
        "scene_detect",
        "coarse_ocr",
        "transcribe",
    )
    assert pipeline.stage("visual_describe").depends_on == ("keyframe_extract",)
    assert pipeline.stage("summarize").depends_on == (
        "probe",
        "subtitle_extract",
        "scene_detect",
        "content_plan",
        "keyframe_extract",
        "ocr",
        "transcribe",
        "visual_describe",
    )
    assert pipeline.required_artifacts[-1] == "agent_document"
    assert pipeline.stage("summarize").outputs[0].schema_version == "2"


def test_understand_v2_normalizes_xai_defaults_and_stage_capabilities() -> None:
    pipeline = get_pipeline("understand", "2")
    options = pipeline.normalize_options(
        {
            "transcription_provider": "xai",
            "planning_provider": "xai",
            "vision_provider": "xai",
            "summary_provider": "xai",
        }
    )

    assert options["transcription_model"] == "grok-transcribe"
    assert options["planning_model"] == "grok-4.5"
    assert options["vision_model"] == "grok-4.5"
    assert options["summary_model"] == "grok-4.5"
    assert pipeline.stage("transcribe").processor == "ai_transcribe"
    assert required_capabilities_for_stage(pipeline.stage("transcribe"), options) == {"providers": ["xai"]}
    assert required_capabilities_for_stage(pipeline.stage("summarize"), options) == {"providers": ["xai"]}


def test_understand_v1_remains_addressable_as_the_baseline() -> None:
    pipeline = get_pipeline("understand", "1")

    assert pipeline.version == "1"
    assert [stage.name for stage in pipeline.stages] == [
        "probe",
        "audio_extract",
        "subtitle_extract",
        "scene_detect",
        "keyframe_extract",
        "ocr",
        "transcribe",
        "visual_describe",
        "summarize",
    ]
    assert pipeline.normalize_options({})["max_keyframes"] == 12

    request = JobCreateRequest.model_validate(
        {
            "asset_id": str(uuid.uuid4()),
            "pipeline": "understand",
            "pipeline_version": "1",
        }
    )
    assert request.pipeline_version == "1"


def test_unknown_pipeline_is_rejected() -> None:
    with pytest.raises(UnsupportedPipeline):
        get_pipeline("client-supplied-command")


def test_transcode_pipeline_rejects_unknown_options() -> None:
    with pytest.raises(ValidationError):
        get_pipeline("transcode").normalize_options({"ffmpeg_command": "anything"})


def test_v2_job_does_not_accept_undelivered_callback_contract() -> None:
    with pytest.raises(ValidationError):
        JobCreateRequest.model_validate(
            {
                "asset_id": str(uuid.uuid4()),
                "pipeline": "transcode",
                "callback_url": "https://example.test/callback",
            }
        )


def test_v2_job_accepts_registered_webhook_selection_or_explicit_disable() -> None:
    endpoint_id = uuid.uuid4()
    selected = JobCreateRequest.model_validate(
        {
            "asset_id": str(uuid.uuid4()),
            "pipeline": "transcode",
            "webhook": {"endpoint_id": str(endpoint_id)},
        }
    )
    disabled = JobCreateRequest.model_validate(
        {
            "asset_id": str(uuid.uuid4()),
            "pipeline": "transcode",
            "webhook": False,
        }
    )

    assert selected.webhook.endpoint_id == endpoint_id
    assert disabled.webhook is False


def test_v2_job_rejects_arbitrary_per_request_webhook_url() -> None:
    with pytest.raises(ValidationError):
        JobCreateRequest.model_validate(
            {
                "asset_id": str(uuid.uuid4()),
                "pipeline": "transcode",
                "webhook": {"url": "https://example.test/callback"},
            }
        )


def test_reingesting_expired_content_refreshes_response_state(tmp_path, monkeypatch) -> None:
    now = datetime.now(UTC)
    existing = Blob(
        id=uuid.uuid4(),
        sha256="a" * 64,
        size_bytes=4,
        media_type="video/mp4",
        bucket="media",
        object_key=f"blobs/sha256/aa/{'a' * 64}",
        state="expired",
        expires_at=now - timedelta(hours=1),
        expired_at=now,
    )
    staged_path = tmp_path / "staged.upload"
    staged_path.write_bytes(b"test")

    class FakeStore:
        async def upload_file(self, *args, **kwargs) -> StoredObject:
            return StoredObject(bucket="media", key=existing.object_key, etag="etag")

    class FakeResult:
        def scalar_one(self):
            return existing.id

    class FakeSession:
        def __init__(self) -> None:
            self.scalar_calls = 0
            self.asset = None
            self.refreshed = False

        async def scalar(self, statement):
            self.scalar_calls += 1
            if self.scalar_calls == 1:
                return existing
            self.asset.blob = existing
            return self.asset

        async def execute(self, statement):
            return FakeResult()

        def add(self, asset) -> None:
            self.asset = asset

        async def commit(self) -> None:
            pass

        async def refresh(self, instance) -> None:
            assert instance is existing
            self.refreshed = True
            instance.state = "available"

        async def rollback(self) -> None:
            pass

    settings = Settings(s3_bucket="media", temp_dir=tmp_path)
    service = AssetService(settings, FakeStore())

    async def stage_upload(upload) -> StagedUpload:
        return StagedUpload(path=staged_path, sha256="a" * 64, size_bytes=4)

    monkeypatch.setattr(service, "_stage_upload", stage_upload)
    session = FakeSession()
    upload = SimpleNamespace(filename="sample.mp4", content_type="video/mp4")

    result = asyncio.run(
        service.ingest_upload(
            session,
            upload,
            client_id=uuid.uuid4(),
            source_metadata={},
        )
    )

    assert session.refreshed
    assert result.asset.blob.state == "available"
    assert not result.duplicate_content
