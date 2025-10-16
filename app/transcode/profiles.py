from __future__ import annotations

from dataclasses import dataclass
from typing import Dict

from ..models import CodecPreference, QualityTarget


@dataclass(frozen=True)
class QualityProfile:
    name: QualityTarget
    width: int
    height: int
    video_bitrate: int  # bits per second
    codec: CodecPreference


PROFILES: Dict[QualityTarget, QualityProfile] = {
    QualityTarget.uhd_2160p: QualityProfile(
        name=QualityTarget.uhd_2160p,
        width=3840,
        height=2160,
        video_bitrate=8_000_000,
        codec=CodecPreference.h265,
    ),
    QualityTarget.fhd_1080p: QualityProfile(
        name=QualityTarget.fhd_1080p,
        width=1920,
        height=1080,
        video_bitrate=8_000_000,
        codec=CodecPreference.h264,
    ),
    QualityTarget.hd_720p: QualityProfile(
        name=QualityTarget.hd_720p,
        width=1280,
        height=720,
        video_bitrate=4_000_000,
        codec=CodecPreference.h264,
    ),
    QualityTarget.sd_480p: QualityProfile(
        name=QualityTarget.sd_480p,
        width=848,
        height=480,
        video_bitrate=2_500_000,
        codec=CodecPreference.h264,
    ),
}


def _best_fit_profile(source_width: int, source_height: int) -> QualityProfile:
    """Return the highest profile that does not exceed the source resolution."""

    sorted_profiles = sorted(PROFILES.values(), key=lambda p: p.height, reverse=True)
    for profile in sorted_profiles:
        if source_height >= profile.height or source_width >= profile.width:
            return profile
    return PROFILES[QualityTarget.sd_480p]


def choose_profile(source_width: int, source_height: int, request: QualityTarget) -> QualityProfile:
    """Choose the best profile based on source dimensions and requested preset."""

    if request == QualityTarget.audio_only:
        raise ValueError("Audio-only requests do not use video profiles")

    if request != QualityTarget.auto:
        desired = PROFILES[request]
        if source_height >= desired.height or source_width >= desired.width:
            return desired
        return _best_fit_profile(source_width, source_height)

    return _best_fit_profile(source_width, source_height)
