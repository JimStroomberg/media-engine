from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, GetJsonSchemaHandler, model_validator
from pydantic.json_schema import JsonSchemaValue
from pydantic_core import CoreSchema

from ..models import CodecPreference, EncodingQuality, QualityTarget


class UnsupportedPipeline(ValueError):
    pass


@dataclass(frozen=True)
class PipelineArtifactDefinition:
    artifact_type: str
    required: bool = True
    schema_version: str = "1"


@dataclass(frozen=True)
class PipelineProviderSelectionDefinition:
    provider_option: str
    model_option: str
    supported_providers: tuple[str, ...]


@dataclass(frozen=True)
class PipelineStageDefinition:
    name: str
    processor: str
    required_capabilities: Mapping[str, tuple[str, ...]]
    provider_selection: PipelineProviderSelectionDefinition | None = None
    depends_on: tuple[str, ...] = ()
    outputs: tuple[PipelineArtifactDefinition, ...] = ()

    @property
    def allowed_artifact_types(self) -> frozenset[str]:
        return frozenset(output.artifact_type for output in self.outputs)

    @property
    def required_artifact_types(self) -> frozenset[str]:
        return frozenset(output.artifact_type for output in self.outputs if output.required)


@dataclass(frozen=True)
class PipelineDefinition:
    name: str
    version: str
    schema_version: str
    processor_versions: Mapping[str, str]
    options_model: type[BaseModel]
    stages: tuple[PipelineStageDefinition, ...]

    def normalize_options(self, options: Mapping[str, Any]) -> dict[str, Any]:
        """Validate options and include defaults before computing run identity."""

        return self.options_model.model_validate(options).model_dump(mode="json")

    def stage(self, name: str) -> PipelineStageDefinition:
        try:
            return next(stage for stage in self.stages if stage.name == name)
        except StopIteration as exc:
            raise UnsupportedPipeline(f"Pipeline {self.name} v{self.version} has no stage {name}") from exc

    @property
    def required_artifacts(self) -> tuple[str, ...]:
        return tuple(output.artifact_type for stage in self.stages for output in stage.outputs if output.required)

    @property
    def supported_providers(self) -> tuple[str, ...]:
        providers = {provider for stage in self.stages for provider in stage.required_capabilities.get("providers", ())}
        for stage in self.stages:
            if stage.provider_selection is not None:
                providers.update(stage.provider_selection.supported_providers)
        return tuple(sorted(providers))


class TranscodeOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    quality: Literal[
        "auto",
        "uhd_2160p",
        "fhd_1080p",
        "hd_720p",
        "sd_480p",
        "audio_only",
    ] = "auto"
    codec: CodecPreference = CodecPreference.auto


class TranscodeV2Options(TranscodeOptions):
    quality: QualityTarget = QualityTarget.auto
    quality_profile: EncodingQuality = EncodingQuality.balanced


class MediaPreparationOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    audio_codec: Literal["mp3"] = "mp3"
    audio_bitrate_kbps: int = Field(48, ge=24, le=128)
    scene_threshold: float = Field(0.35, ge=0.05, le=0.95)
    keyframe_interval_seconds: float = Field(30.0, ge=1.0, le=600.0)
    max_keyframes: int = Field(12, ge=1, le=100)
    keyframe_max_width: int = Field(1280, ge=320, le=3840)
    ocr_language: str = Field("eng", min_length=3, max_length=64, pattern=r"^[a-zA-Z0-9_+.-]+$")


class UnderstandOptions(MediaPreparationOptions):
    transcription_provider: Literal["openai"] = "openai"
    transcription_model: str = Field("gpt-4o-transcribe-diarize", min_length=1, max_length=128)
    vision_provider: Literal["openai"] = "openai"
    vision_model: str = Field("gpt-5.6-sol", min_length=1, max_length=128)
    vision_detail: Literal["low", "high"] = "high"
    summary_provider: Literal["openai"] = "openai"
    summary_model: str = Field("gpt-5.6-terra", min_length=1, max_length=128)


UNDERSTAND_PROVIDER_MODELS: Mapping[str, Mapping[str, str]] = MappingProxyType(
    {
        "openai": MappingProxyType(
            {
                "transcription": "gpt-4o-transcribe-diarize",
                "planning": "gpt-5.6-terra",
                "vision": "gpt-5.6-sol",
                "summary": "gpt-5.6-terra",
            }
        ),
        "xai": MappingProxyType(
            {
                "transcription": "grok-transcribe",
                "planning": "grok-4.5",
                "vision": "grok-4.5",
                "summary": "grok-4.5",
            }
        ),
    }
)


class UnderstandV2Options(UnderstandOptions):
    _DYNAMIC_DEFAULT_FIELDS: ClassVar[frozenset[str]] = frozenset(
        {
            "transcription_provider",
            "transcription_model",
            "planning_provider",
            "planning_model",
            "vision_provider",
            "vision_model",
            "summary_provider",
            "summary_model",
        }
    )

    transcription_provider: Literal["openai", "xai"] = "xai"
    vision_provider: Literal["openai", "xai"] = "xai"
    summary_provider: Literal["openai", "xai"] = "xai"
    analysis_profile: Literal["auto", "meme", "tutorial", "talk", "action", "general"] = "auto"
    planning_provider: Literal["openai", "xai"] = "xai"
    planning_model: str = Field("grok-4.5", min_length=1, max_length=128)
    coarse_max_keyframes: int = Field(12, ge=1, le=24)
    targeted_keyframes: int = Field(12, ge=0, le=48)
    max_keyframes: int = Field(24, ge=1, le=100)

    @model_validator(mode="before")
    @classmethod
    def _provider_model_defaults(cls, value: Any) -> Any:
        if not isinstance(value, Mapping):
            return value
        options = dict(value)
        for stage in ("transcription", "planning", "vision", "summary"):
            provider = str(options.get(f"{stage}_provider", "xai"))
            defaults = UNDERSTAND_PROVIDER_MODELS.get(provider)
            if defaults is not None:
                options.setdefault(f"{stage}_model", defaults[stage])
        return options

    @classmethod
    def __get_pydantic_json_schema__(
        cls,
        core_schema: CoreSchema,
        handler: GetJsonSchemaHandler,
    ) -> JsonSchemaValue:
        schema = super().__get_pydantic_json_schema__(core_schema, handler)
        schema = handler.resolve_ref_schema(schema)
        properties = schema.get("properties", {})
        for field_name in cls._DYNAMIC_DEFAULT_FIELDS:
            field_schema = properties.get(field_name)
            if isinstance(field_schema, dict):
                field_schema.pop("default", None)
        return schema


_LOCAL_CAPABILITIES = MappingProxyType({"processors": ("ffmpeg", "tesseract")})
_OPENAI_CAPABILITIES = MappingProxyType({"providers": ("openai",)})
_NO_CAPABILITIES: Mapping[str, tuple[str, ...]] = MappingProxyType({})
_UNDERSTAND_PROVIDERS = tuple(UNDERSTAND_PROVIDER_MODELS)


def _provider_selection(stage: str) -> PipelineProviderSelectionDefinition:
    return PipelineProviderSelectionDefinition(
        provider_option=f"{stage}_provider",
        model_option=f"{stage}_model",
        supported_providers=_UNDERSTAND_PROVIDERS,
    )


def required_capabilities_for_stage(
    stage: PipelineStageDefinition,
    options: Mapping[str, Any],
) -> dict[str, list[str]]:
    """Resolve option-driven provider requirements for a concrete stage run."""

    capabilities = {key: list(values) for key, values in stage.required_capabilities.items()}
    if stage.provider_selection is not None:
        capabilities["providers"] = [str(options[stage.provider_selection.provider_option])]
    return capabilities


def _media_foundation_stages() -> tuple[PipelineStageDefinition, ...]:
    return (
        PipelineStageDefinition(
            name="probe",
            processor="probe",
            required_capabilities=MappingProxyType({"processors": ("ffmpeg",)}),
            outputs=(PipelineArtifactDefinition("media_metadata"),),
        ),
        PipelineStageDefinition(
            name="audio_extract",
            processor="audio_extract",
            required_capabilities=MappingProxyType({"processors": ("ffmpeg",)}),
            depends_on=("probe",),
            outputs=(
                PipelineArtifactDefinition("audio_metadata"),
                PipelineArtifactDefinition("audio", required=False),
            ),
        ),
        PipelineStageDefinition(
            name="subtitle_extract",
            processor="subtitle_extract",
            required_capabilities=MappingProxyType({"processors": ("ffmpeg",)}),
            depends_on=("probe",),
            outputs=(PipelineArtifactDefinition("subtitles"),),
        ),
        PipelineStageDefinition(
            name="scene_detect",
            processor="scene_detect",
            required_capabilities=MappingProxyType({"processors": ("ffmpeg",)}),
            depends_on=("probe",),
            outputs=(PipelineArtifactDefinition("scenes"),),
        ),
    )


def _media_preparation_stages() -> tuple[PipelineStageDefinition, ...]:
    return _media_foundation_stages() + (
        PipelineStageDefinition(
            name="keyframe_extract",
            processor="keyframe_extract",
            required_capabilities=MappingProxyType({"processors": ("ffmpeg",)}),
            depends_on=("scene_detect",),
            outputs=(
                PipelineArtifactDefinition("keyframes"),
                PipelineArtifactDefinition("keyframe_index"),
            ),
        ),
        PipelineStageDefinition(
            name="ocr",
            processor="ocr",
            required_capabilities=_LOCAL_CAPABILITIES,
            depends_on=("keyframe_extract",),
            outputs=(PipelineArtifactDefinition("ocr"),),
        ),
    )


_AI_PREPARE_STAGES = _media_preparation_stages()
_UNDERSTAND_STAGES = _AI_PREPARE_STAGES + (
    PipelineStageDefinition(
        name="transcribe",
        processor="openai_transcribe",
        required_capabilities=_OPENAI_CAPABILITIES,
        depends_on=("audio_extract",),
        outputs=(PipelineArtifactDefinition("transcript"),),
    ),
    PipelineStageDefinition(
        name="visual_describe",
        processor="openai_visual_describe",
        required_capabilities=_OPENAI_CAPABILITIES,
        depends_on=("keyframe_extract",),
        outputs=(PipelineArtifactDefinition("visual_descriptions"),),
    ),
    PipelineStageDefinition(
        name="summarize",
        processor="openai_summarize",
        required_capabilities=_OPENAI_CAPABILITIES,
        depends_on=("probe", "subtitle_extract", "scene_detect", "ocr", "transcribe", "visual_describe"),
        outputs=(PipelineArtifactDefinition("agent_document"),),
    ),
)


_UNDERSTAND_V2_STAGES = _media_foundation_stages() + (
    PipelineStageDefinition(
        name="coarse_keyframe_extract",
        processor="coarse_keyframe_extract",
        required_capabilities=MappingProxyType({"processors": ("ffmpeg",)}),
        depends_on=("scene_detect",),
        outputs=(
            PipelineArtifactDefinition("coarse_keyframes"),
            PipelineArtifactDefinition("coarse_keyframe_index"),
        ),
    ),
    PipelineStageDefinition(
        name="coarse_ocr",
        processor="coarse_ocr",
        required_capabilities=_LOCAL_CAPABILITIES,
        depends_on=("coarse_keyframe_extract",),
        outputs=(PipelineArtifactDefinition("coarse_ocr"),),
    ),
    PipelineStageDefinition(
        name="transcribe",
        processor="ai_transcribe",
        required_capabilities=_NO_CAPABILITIES,
        provider_selection=_provider_selection("transcription"),
        depends_on=("audio_extract",),
        outputs=(PipelineArtifactDefinition("transcript"),),
    ),
    PipelineStageDefinition(
        name="content_plan",
        processor="ai_content_plan",
        required_capabilities=_NO_CAPABILITIES,
        provider_selection=_provider_selection("planning"),
        depends_on=("probe", "subtitle_extract", "scene_detect", "coarse_ocr", "transcribe"),
        outputs=(PipelineArtifactDefinition("content_plan"),),
    ),
    PipelineStageDefinition(
        name="keyframe_extract",
        processor="adaptive_keyframe_extract",
        required_capabilities=MappingProxyType({"processors": ("ffmpeg",)}),
        depends_on=("scene_detect", "content_plan"),
        outputs=(
            PipelineArtifactDefinition("keyframes"),
            PipelineArtifactDefinition("keyframe_index"),
        ),
    ),
    PipelineStageDefinition(
        name="ocr",
        processor="ocr",
        required_capabilities=_LOCAL_CAPABILITIES,
        depends_on=("keyframe_extract",),
        outputs=(PipelineArtifactDefinition("ocr"),),
    ),
    PipelineStageDefinition(
        name="visual_describe",
        processor="ai_visual_describe",
        required_capabilities=_NO_CAPABILITIES,
        provider_selection=_provider_selection("vision"),
        depends_on=("keyframe_extract",),
        outputs=(PipelineArtifactDefinition("visual_descriptions"),),
    ),
    PipelineStageDefinition(
        name="summarize",
        processor="ai_summarize_v2",
        required_capabilities=_NO_CAPABILITIES,
        provider_selection=_provider_selection("summary"),
        depends_on=(
            "probe",
            "subtitle_extract",
            "scene_detect",
            "content_plan",
            "keyframe_extract",
            "ocr",
            "transcribe",
            "visual_describe",
        ),
        outputs=(PipelineArtifactDefinition("agent_document", schema_version="2"),),
    ),
)


_PIPELINES = {
    ("transcode", "1"): PipelineDefinition(
        name="transcode",
        version="1",
        schema_version="1",
        processor_versions=MappingProxyType({"transcode-contract": "2"}),
        options_model=TranscodeOptions,
        stages=(
            PipelineStageDefinition(
                name="transcode",
                processor="transcode",
                required_capabilities=MappingProxyType({"pipelines": ("transcode",)}),
                outputs=(PipelineArtifactDefinition("transcoded_media"),),
            ),
        ),
    ),
    ("transcode", "2"): PipelineDefinition(
        name="transcode",
        version="2",
        schema_version="1",
        processor_versions=MappingProxyType({"transcode-contract": "2"}),
        options_model=TranscodeV2Options,
        stages=(
            PipelineStageDefinition(
                name="transcode",
                processor="transcode",
                required_capabilities=MappingProxyType({"pipelines": ("transcode",)}),
                outputs=(PipelineArtifactDefinition("transcoded_media"),),
            ),
        ),
    ),
    ("ai_prepare", "1"): PipelineDefinition(
        name="ai_prepare",
        version="1",
        schema_version="1",
        processor_versions=MappingProxyType(
            {
                "media-probe": "1",
                "audio-extract": "1",
                "subtitle-extract": "1",
                "scene-detect": "1",
                "keyframe-extract": "1",
                "tesseract-ocr": "1",
            }
        ),
        options_model=MediaPreparationOptions,
        stages=_AI_PREPARE_STAGES,
    ),
    ("understand", "1"): PipelineDefinition(
        name="understand",
        version="1",
        schema_version="1",
        processor_versions=MappingProxyType(
            {
                "media-probe": "1",
                "audio-extract": "1",
                "subtitle-extract": "1",
                "scene-detect": "1",
                "keyframe-extract": "1",
                "tesseract-ocr": "1",
                "openai-transcription": "1",
                "openai-vision": "1",
                "agent-document": "1",
            }
        ),
        options_model=UnderstandOptions,
        stages=_UNDERSTAND_STAGES,
    ),
    ("understand", "2"): PipelineDefinition(
        name="understand",
        version="2",
        schema_version="2",
        processor_versions=MappingProxyType(
            {
                "media-probe": "1",
                "audio-extract": "1",
                "subtitle-extract": "1",
                "scene-detect": "1",
                "coarse-keyframe-extract": "1",
                "tesseract-ocr": "1",
                "openai-transcription": "1",
                "content-plan": "1",
                "adaptive-keyframe-extract": "1",
                "openai-vision": "1",
                "agent-document": "2",
            }
        ),
        options_model=UnderstandV2Options,
        stages=_UNDERSTAND_V2_STAGES,
    ),
}

_LATEST_PIPELINE_VERSIONS = {"transcode": "2", "ai_prepare": "1", "understand": "2"}


def get_pipeline(name: str, version: str | None = None) -> PipelineDefinition:
    """Resolve a server-owned pipeline definition by public name and version."""

    resolved_version = version or _LATEST_PIPELINE_VERSIONS.get(name)
    if resolved_version is None:
        raise UnsupportedPipeline(f"Unsupported pipeline: {name}")
    try:
        return _PIPELINES[(name, resolved_version)]
    except KeyError as exc:
        raise UnsupportedPipeline(f"Unsupported pipeline: {name} v{resolved_version}") from exc


def list_pipelines() -> tuple[PipelineDefinition, ...]:
    return tuple(get_pipeline(name) for name in sorted(_LATEST_PIPELINE_VERSIONS))


def pipeline_key_input(definition: PipelineDefinition, options: Mapping[str, Any]) -> dict[str, Any]:
    """Return the versioned portion of a pipeline run-key document."""

    return {
        "pipeline_name": definition.name,
        "pipeline_version": definition.version,
        "schema_version": definition.schema_version,
        "processor_versions": dict(definition.processor_versions),
        "options": dict(options),
    }
