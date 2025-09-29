from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ..config import get_settings


@dataclass(frozen=True)
class MediaStreamInfo:
    codec_type: str
    codec_name: Optional[str]
    width: Optional[int] = None
    height: Optional[int] = None
    bit_rate: Optional[int] = None


@dataclass(frozen=True)
class MediaInfo:
    container: Optional[str]
    bit_rate: Optional[int]
    duration: Optional[float]
    video: Optional[MediaStreamInfo]
    audio: Optional[MediaStreamInfo]


class ProbeError(RuntimeError):
    pass


def probe_media(path: Path) -> MediaInfo:
    settings = get_settings()
    cmd = [
        settings.ffprobe_command,
        "-v",
        "error",
        "-show_entries",
        "stream=index,codec_type,codec_name,width,height,bit_rate:format=format_name,bit_rate,duration",
        "-print_format",
        "json",
        str(path),
    ]

    try:
        result = subprocess.run(cmd, check=True, capture_output=True, text=True)
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:  # noqa: BLE001
        raise ProbeError(f"Failed to probe media: {exc}") from exc

    payload = json.loads(result.stdout or "{}")
    streams = payload.get("streams", [])
    video_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio_stream = next((s for s in streams if s.get("codec_type") == "audio"), None)

    video = (
        MediaStreamInfo(
            codec_type="video",
            codec_name=video_stream.get("codec_name"),
            width=int(video_stream.get("width")) if video_stream.get("width") else None,
            height=int(video_stream.get("height")) if video_stream.get("height") else None,
            bit_rate=int(video_stream.get("bit_rate")) if video_stream.get("bit_rate") else None,
        )
        if video_stream
        else None
    )

    audio = (
        MediaStreamInfo(
            codec_type="audio",
            codec_name=audio_stream.get("codec_name"),
            bit_rate=int(audio_stream.get("bit_rate")) if audio_stream.get("bit_rate") else None,
        )
        if audio_stream
        else None
    )

    format_section = payload.get("format", {})
    bit_rate = int(format_section.get("bit_rate")) if format_section.get("bit_rate") else None
    duration = float(format_section.get("duration")) if format_section.get("duration") else None
    container = format_section.get("format_name")

    return MediaInfo(container=container, bit_rate=bit_rate, duration=duration, video=video, audio=audio)
