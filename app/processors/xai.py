from __future__ import annotations

import asyncio
import json
import logging
import math
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from openai import AsyncOpenAI

from ..config import Settings
from .openai import (
    OpenAIContentPlanningProvider,
    OpenAIMediaProcessor,
    OpenAISummaryProvider,
    OpenAIVisionProvider,
)
from .usage import record_provider_usage

logger = logging.getLogger(__name__)


class XAIConfigurationError(RuntimeError):
    pass


class XAITranscriptionProvider:
    """Normalize xAI word-level STT into Media Engine transcript segments."""

    REST_RATE_USD_PER_HOUR = 0.10
    SEGMENT_PAUSE_SECONDS = 0.5

    def __init__(self, client: httpx.Client) -> None:
        self.client = client

    async def transcribe(
        self,
        audio_path: Path,
        *,
        model: str,
        output_dir: Path,
        duration_seconds: float | None,
        bitrate_kbps: int,
    ) -> dict[str, Any]:
        del output_dir, bitrate_kbps
        started_at = datetime.now(UTC)
        raw = await asyncio.to_thread(self._request, audio_path)
        text = str(raw.get("text") or "").strip()
        words = self._valid_words(raw.get("words"))
        resolved_duration = self._duration(raw.get("duration"), duration_seconds)
        segments = self._segments(words)
        if not segments and text:
            segments = [
                {
                    "id": "segment-0",
                    "start_seconds": 0.0,
                    "end_seconds": resolved_duration,
                    "speaker": None,
                    "text": text,
                }
            ]
        estimated_cost = resolved_duration / 3600 * self.REST_RATE_USD_PER_HOUR
        logger.info(
            "AI response usage provider=xai model=%s usage=%s",
            model,
            json.dumps(
                {
                    "type": "duration",
                    "duration_seconds": resolved_duration,
                    "rate_usd_per_hour": self.REST_RATE_USD_PER_HOUR,
                    "estimated_cost_usd": estimated_cost,
                },
                separators=(",", ":"),
                sort_keys=True,
            ),
        )
        record_provider_usage(
            provider="xai",
            model=model,
            operation="transcription",
            usage={
                "type": "duration",
                "duration_seconds": resolved_duration,
                "rate_usd_per_hour": self.REST_RATE_USD_PER_HOUR,
                "estimated_cost_usd": estimated_cost,
            },
            started_at=started_at,
        )
        return {
            "schema_version": "1",
            "provider": "xai",
            "model": model,
            "text": text,
            "segments": segments,
            "chunk_count": 1,
            "usage": [
                {
                    "type": "duration",
                    "duration_seconds": resolved_duration,
                    "rate_usd_per_hour": self.REST_RATE_USD_PER_HOUR,
                    "estimated_cost_usd": estimated_cost,
                }
            ],
        }

    def _request(self, audio_path: Path) -> dict[str, Any]:
        with audio_path.open("rb") as audio_file:
            response = self.client.post(
                "stt",
                data={"diarize": "true", "filler_words": "true"},
                files={"file": (audio_path.name, audio_file, "audio/mpeg")},
            )
        response.raise_for_status()
        result: dict[str, Any] = response.json()
        return result

    @staticmethod
    def _duration(value: Any, fallback: float | None) -> float:
        try:
            duration = float(value)
        except (TypeError, ValueError):
            duration = float(fallback or 0.0)
        return duration if math.isfinite(duration) and duration >= 0 else 0.0

    @staticmethod
    def _valid_words(value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        words: list[dict[str, Any]] = []
        for item in value:
            if not isinstance(item, dict):
                continue
            try:
                start = float(item["start"])
                end = float(item["end"])
            except (KeyError, TypeError, ValueError):
                continue
            text = str(item.get("text") or "").strip()
            if not text or not all(math.isfinite(timestamp) for timestamp in (start, end)) or start < 0 or end < start:
                continue
            words.append({"text": text, "start": start, "end": end, "speaker": item.get("speaker")})
        return sorted(words, key=lambda item: (item["start"], item["end"]))

    @classmethod
    def _segments(cls, words: list[dict[str, Any]]) -> list[dict[str, Any]]:
        groups: list[list[dict[str, Any]]] = []
        current: list[dict[str, Any]] = []
        for word in words:
            previous = current[-1] if current else None
            speaker_changed = previous is not None and word.get("speaker") != previous.get("speaker")
            pause = float(word["start"]) - float(previous["end"]) if previous is not None else 0.0
            sentence_pause = previous is not None and str(previous["text"]).endswith((".", "?", "!")) and pause >= 0.3
            if current and (speaker_changed or pause > cls.SEGMENT_PAUSE_SECONDS or sentence_pause):
                groups.append(current)
                current = []
            current.append(word)
        if current:
            groups.append(current)

        segments: list[dict[str, Any]] = []
        for index, group in enumerate(groups):
            speaker = group[0].get("speaker")
            segments.append(
                {
                    "id": f"chunk-0-segment-{index}",
                    "start_seconds": float(group[0]["start"]),
                    "end_seconds": float(group[-1]["end"]),
                    "speaker": f"speaker-{speaker}" if speaker is not None else None,
                    "text": " ".join(str(word["text"]) for word in group),
                }
            )
        return segments


class XAIMediaProcessor(OpenAIMediaProcessor):
    """xAI implementation of the provider-neutral AI stage processors."""

    def __init__(
        self,
        settings: Settings,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout_seconds: float | None = None,
        max_retries: int | None = None,
    ) -> None:
        resolved_key = api_key or settings.xai_api_key
        if not resolved_key:
            raise XAIConfigurationError("XAI_API_KEY is required for xAI-backed stages")
        resolved_base_url = f"{(base_url or settings.xai_base_url).rstrip('/')}/"
        resolved_timeout = timeout_seconds or settings.xai_timeout_seconds
        responses_client = AsyncOpenAI(
            api_key=resolved_key,
            base_url=resolved_base_url,
            timeout=resolved_timeout,
            max_retries=settings.xai_max_retries if max_retries is None else max_retries,
        )
        stt_client = httpx.Client(
            base_url=resolved_base_url,
            headers={"Authorization": f"Bearer {resolved_key}"},
            timeout=httpx.Timeout(resolved_timeout, connect=10.0),
        )
        self._client = responses_client
        self._stt_client = stt_client
        self.transcription = XAITranscriptionProvider(stt_client)
        self.vision = OpenAIVisionProvider(
            responses_client,
            provider_name="xai",
            reasoning_effort="low",
        )
        self.planning = OpenAIContentPlanningProvider(
            responses_client,
            provider_name="xai",
            reasoning_effort="low",
        )
        self.summary = OpenAISummaryProvider(
            responses_client,
            provider_name="xai",
            reasoning_effort="low",
        )

    async def close(self) -> None:
        await self._client.close()
        await asyncio.to_thread(self._stt_client.close)
