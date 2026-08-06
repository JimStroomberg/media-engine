from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api.auth import get_credential_cipher
from app.api.v2 import (
    JobCreateRequest,
    database_session,
    parse_source_metadata,
    response_from_pipeline,
    runtime_pipeline_response,
)
from app.config import Settings, get_settings
from app.domain.pipelines import (
    UnsupportedPipeline,
    get_pipeline,
    list_pipelines,
    required_capabilities_for_stage,
)
from app.main import app
from app.persistence.models import Base, Blob
from app.services.assets import AssetService, StagedUpload
from app.services.providers import ProviderConfigurationService
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

    assert pipeline.version == "2"
    assert pipeline.required_artifacts == ("transcoded_media",)
    assert pipeline.normalize_options({}) == {
        "quality": "auto",
        "codec": "auto",
        "quality_profile": "balanced",
    }
    transcode_v1 = get_pipeline("transcode", "1")
    assert transcode_v1.normalize_options({}) == {"quality": "auto", "codec": "auto"}
    with pytest.raises(ValidationError):
        transcode_v1.normalize_options({"quality": "qhd_1440p"})
    assert [item.name for item in list_pipelines()] == ["ai_prepare", "transcode", "understand"]


def test_legacy_transcode_form_advertises_quality_profile() -> None:
    openapi = app.openapi()
    request_schema = openapi["components"]["schemas"]["Body_submit_job_jobs_post"]

    assert request_schema["properties"]["quality_profile"] == {
        "$ref": "#/components/schemas/EncodingQuality",
        "default": "balanced",
    }


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
    assert options["transcription_model"] == "grok-transcribe"
    assert options["planning_model"] == "grok-4.5"
    assert options["vision_model"] == "grok-4.5"
    assert options["summary_model"] == "grok-4.5"
    assert options["analysis_profile"] == "auto"
    assert options["coarse_max_keyframes"] == 12
    assert options["targeted_keyframes"] == 12
    assert options["max_keyframes"] == 24
    assert pipeline.stage("transcribe").required_capabilities == {}
    assert pipeline.stage("transcribe").provider_selection is not None
    assert pipeline.stage("transcribe").provider_selection.supported_providers == ("openai", "xai")
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


@pytest.mark.parametrize(
    ("provider", "expected_models"),
    [
        (
            "openai",
            {
                "transcription": "gpt-4o-transcribe-diarize",
                "planning": "gpt-5.6-terra",
                "vision": "gpt-5.6-sol",
                "summary": "gpt-5.6-terra",
            },
        ),
        (
            "xai",
            {
                "transcription": "grok-transcribe",
                "planning": "grok-4.5",
                "vision": "grok-4.5",
                "summary": "grok-4.5",
            },
        ),
    ],
)
def test_understand_v2_normalizes_provider_defaults_and_stage_capabilities(
    provider: str,
    expected_models: dict[str, str],
) -> None:
    pipeline = get_pipeline("understand", "2")
    options = pipeline.normalize_options(
        {
            "transcription_provider": provider,
            "planning_provider": provider,
            "vision_provider": provider,
            "summary_provider": provider,
        }
    )

    for stage, expected_model in expected_models.items():
        assert options[f"{stage}_model"] == expected_model
    assert pipeline.stage("transcribe").processor == "ai_transcribe"
    assert required_capabilities_for_stage(pipeline.stage("transcribe"), options) == {"providers": [provider]}
    assert required_capabilities_for_stage(pipeline.stage("summarize"), options) == {"providers": [provider]}


def test_understand_v2_discovery_separates_dynamic_defaults_from_json_schema() -> None:
    pipeline = get_pipeline("understand", "2")
    effective_options = pipeline.normalize_options({})
    discovery = response_from_pipeline(
        pipeline,
        effective_options=effective_options,
        available_providers={"openai", "xai"},
    )

    for stage in ("transcription", "planning", "vision", "summary"):
        assert "default" not in discovery.options_schema["properties"][f"{stage}_provider"]
        assert "default" not in discovery.options_schema["properties"][f"{stage}_model"]
    assert discovery.effective_options == effective_options
    assert discovery.available_providers == ["openai", "xai"]

    selectable_stages = [stage for stage in discovery.stages if stage.provider_selection is not None]
    assert [stage.name for stage in selectable_stages] == [
        "transcribe",
        "content_plan",
        "visual_describe",
        "summarize",
    ]
    for stage in selectable_stages:
        selection = stage.provider_selection
        assert selection is not None
        assert stage.required_capabilities == {}
        assert selection.effective_defaults.provider in selection.supported_providers
        assert selection.effective_defaults.provider == effective_options[selection.provider_option]
        assert selection.effective_defaults.model == effective_options[selection.model_option]
        assert stage.effective_required_capabilities == {"providers": [selection.effective_defaults.provider]}


async def test_understand_v2_runtime_discovery_uses_management_provider_defaults() -> None:
    pipeline = get_pipeline("understand", "2")
    configured_models = {
        "transcription": "configured-openai-transcription",
        "planning": "configured-openai-planning",
        "vision": "configured-openai-vision",
        "summary": "configured-openai-summary",
    }
    provider_configs = [
        SimpleNamespace(provider="openai", is_default=True, models=configured_models),
        SimpleNamespace(
            provider="xai",
            is_default=False,
            models={
                "transcription": "grok-transcribe",
                "planning": "grok-4.5",
                "vision": "grok-4.5",
                "summary": "grok-4.5",
            },
        ),
    ]
    session = SimpleNamespace(scalars=AsyncMock(return_value=provider_configs))
    provider_service = ProviderConfigurationService(SimpleNamespace(), SimpleNamespace())

    discovery = await runtime_pipeline_response(
        pipeline,
        session=session,
        provider_service=provider_service,
        available_providers={"openai", "xai"},
    )

    for stage, configured_model in configured_models.items():
        assert discovery.effective_options[f"{stage}_provider"] == "openai"
        assert discovery.effective_options[f"{stage}_model"] == configured_model
    assert pipeline.normalize_options(discovery.effective_options) == discovery.effective_options
    for stage in discovery.stages:
        if stage.provider_selection is not None:
            assert stage.effective_required_capabilities == {"providers": ["openai"]}


def test_pipeline_discovery_contract_is_published_in_openapi() -> None:
    schemas = app.openapi()["components"]["schemas"]

    assert {
        "options_schema",
        "effective_options",
        "available_providers",
        "stages",
    }.issubset(schemas["PipelineResponse"]["properties"])
    assert {
        "required_capabilities",
        "effective_required_capabilities",
        "provider_selection",
    }.issubset(schemas["PipelineStageContractResponse"]["properties"])
    assert {
        "provider_option",
        "model_option",
        "supported_providers",
        "effective_defaults",
    }.issubset(schemas["PipelineProviderSelectionResponse"]["properties"])


def test_understand_v2_discovery_endpoint_matches_runtime_provider_configuration() -> None:
    configured_models = {
        "transcription": "grok-transcribe",
        "planning": "grok-4.5",
        "vision": "grok-4.5",
        "summary": "grok-4.5",
    }
    session = SimpleNamespace(
        scalars=AsyncMock(
            return_value=[
                SimpleNamespace(provider="openai", is_default=False, models={}),
                SimpleNamespace(provider="xai", is_default=True, models=configured_models),
            ]
        )
    )

    async def fake_database_session():
        yield session

    app.dependency_overrides[database_session] = fake_database_session
    app.dependency_overrides[get_settings] = lambda: Settings(_env_file=None)
    app.dependency_overrides[get_credential_cipher] = lambda: SimpleNamespace()
    try:
        response = TestClient(app).get("/v2/pipelines/understand?version=2")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    discovery = response.json()
    assert discovery["available_providers"] == ["openai", "xai"]
    assert discovery["effective_options"]["transcription_provider"] == "xai"
    assert discovery["effective_options"]["transcription_model"] == "grok-transcribe"
    assert "default" not in discovery["options_schema"]["properties"]["transcription_model"]
    transcribe = next(stage for stage in discovery["stages"] if stage["name"] == "transcribe")
    assert transcribe["required_capabilities"] == {}
    assert transcribe["effective_required_capabilities"] == {"providers": ["xai"]}
    assert transcribe["provider_selection"]["supported_providers"] == ["openai", "xai"]
    assert transcribe["provider_selection"]["effective_defaults"] == {
        "provider": "xai",
        "model": "grok-transcribe",
    }


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
