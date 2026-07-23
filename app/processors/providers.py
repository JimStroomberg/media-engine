from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol


class TranscriptionProvider(Protocol):
    async def transcribe(
        self,
        audio_path: Path,
        *,
        model: str,
        output_dir: Path,
        duration_seconds: float | None,
        bitrate_kbps: int,
    ) -> dict[str, Any]: ...


class VisionProvider(Protocol):
    async def describe(
        self,
        images: list[tuple[float, Path]],
        *,
        model: str,
        detail: str,
    ) -> dict[str, Any]: ...


class ContentPlanningProvider(Protocol):
    async def plan(
        self,
        evidence: dict[str, Any],
        *,
        model: str,
        requested_profile: str,
        target_limit: int,
    ) -> dict[str, Any]: ...


class SummaryProvider(Protocol):
    async def summarize(
        self,
        evidence: dict[str, Any],
        *,
        model: str,
        document_version: str = "1",
    ) -> dict[str, Any]: ...
