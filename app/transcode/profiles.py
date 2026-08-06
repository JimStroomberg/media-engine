from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from ..models import CodecPreference, EncodingQuality, QualityTarget


@dataclass(frozen=True)
class RateControl:
    mode: str
    average_bitrate: int
    peak_bitrate: int
    buffer_size: int


@dataclass(frozen=True)
class QualityProfile:
    name: QualityTarget
    width: int
    height: int
    codec: CodecPreference
    balanced_rate_control: Mapping[CodecPreference, RateControl]


def _rate(average: int, peak: int, buffer_size: int) -> RateControl:
    return RateControl(
        mode="constrained_vbr",
        average_bitrate=average,
        peak_bitrate=peak,
        buffer_size=buffer_size,
    )


def _rates(*, h264: tuple[int, int, int], h265: tuple[int, int, int]) -> Mapping[CodecPreference, RateControl]:
    return MappingProxyType(
        {
            CodecPreference.h264: _rate(*h264),
            CodecPreference.h265: _rate(*h265),
        }
    )


PROFILES: dict[QualityTarget, QualityProfile] = {
    QualityTarget.uhd_2160p: QualityProfile(
        name=QualityTarget.uhd_2160p,
        width=3840,
        height=2160,
        codec=CodecPreference.h265,
        balanced_rate_control=_rates(
            h264=(16_000_000, 22_000_000, 32_000_000),
            h265=(12_000_000, 16_000_000, 24_000_000),
        ),
    ),
    QualityTarget.qhd_1440p: QualityProfile(
        name=QualityTarget.qhd_1440p,
        width=2560,
        height=1440,
        codec=CodecPreference.h265,
        balanced_rate_control=_rates(
            h264=(10_000_000, 14_000_000, 20_000_000),
            h265=(7_500_000, 10_000_000, 15_000_000),
        ),
    ),
    QualityTarget.fhd_1080p: QualityProfile(
        name=QualityTarget.fhd_1080p,
        width=1920,
        height=1080,
        codec=CodecPreference.h264,
        balanced_rate_control=_rates(
            h264=(6_000_000, 8_000_000, 12_000_000),
            h265=(4_500_000, 6_000_000, 9_000_000),
        ),
    ),
    QualityTarget.hd_720p: QualityProfile(
        name=QualityTarget.hd_720p,
        width=1280,
        height=720,
        codec=CodecPreference.h264,
        balanced_rate_control=_rates(
            h264=(3_500_000, 5_000_000, 7_000_000),
            h265=(2_500_000, 3_500_000, 5_000_000),
        ),
    ),
    QualityTarget.sd_480p: QualityProfile(
        name=QualityTarget.sd_480p,
        width=848,
        height=480,
        codec=CodecPreference.h264,
        balanced_rate_control=_rates(
            h264=(1_750_000, 2_500_000, 3_500_000),
            h265=(1_250_000, 1_800_000, 2_500_000),
        ),
    ),
    QualityTarget.low_360p: QualityProfile(
        name=QualityTarget.low_360p,
        width=640,
        height=360,
        codec=CodecPreference.h264,
        balanced_rate_control=_rates(
            h264=(1_000_000, 1_500_000, 2_000_000),
            h265=(750_000, 1_100_000, 1_500_000),
        ),
    ),
}


_QUALITY_FACTORS: Mapping[EncodingQuality, tuple[int, int]] = MappingProxyType(
    {
        EncodingQuality.compact: (2, 3),
        EncodingQuality.balanced: (1, 1),
        EncodingQuality.high: (3, 2),
    }
)


def _scale_bitrate(value: int, numerator: int, denominator: int) -> int:
    scaled = value * numerator / denominator
    return int(round(scaled / 50_000) * 50_000)


def resolve_rate_control(
    profile: QualityProfile,
    codec: CodecPreference,
    quality: EncodingQuality,
) -> RateControl:
    try:
        baseline = profile.balanced_rate_control[codec]
    except KeyError as exc:
        raise ValueError(f"Unsupported output codec for rate control: {codec}") from exc
    numerator, denominator = _QUALITY_FACTORS[quality]
    return RateControl(
        mode=baseline.mode,
        average_bitrate=_scale_bitrate(baseline.average_bitrate, numerator, denominator),
        peak_bitrate=_scale_bitrate(baseline.peak_bitrate, numerator, denominator),
        buffer_size=_scale_bitrate(baseline.buffer_size, numerator, denominator),
    )


def _best_fit_profile(source_width: int, source_height: int) -> QualityProfile:
    """Return the highest profile that does not exceed the source resolution."""

    sorted_profiles = sorted(PROFILES.values(), key=lambda p: p.height, reverse=True)
    for profile in sorted_profiles:
        if source_height >= profile.height or source_width >= profile.width:
            return profile
    return PROFILES[QualityTarget.low_360p]


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
