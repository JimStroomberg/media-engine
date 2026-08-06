from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from app.config import Settings
from app.models import CodecPreference, EncodingQuality, JobStatus, QualityTarget
from app.transcode.engine import TranscodeEngine
from app.transcode.profiles import PROFILES, choose_profile, resolve_rate_control


def test_resolution_profiles_cover_low_through_ultra_hd() -> None:
    assert [profile.height for profile in sorted(PROFILES.values(), key=lambda item: item.height)] == [
        360,
        480,
        720,
        1080,
        1440,
        2160,
    ]
    assert choose_profile(2560, 1440, QualityTarget.auto).name == QualityTarget.qhd_1440p
    assert choose_profile(640, 360, QualityTarget.low_360p).name == QualityTarget.low_360p
    assert choose_profile(320, 240, QualityTarget.auto).name == QualityTarget.low_360p


def test_quality_profiles_scale_one_shared_codec_contract() -> None:
    profile = PROFILES[QualityTarget.fhd_1080p]

    compact = resolve_rate_control(profile, CodecPreference.h264, EncodingQuality.compact)
    balanced = resolve_rate_control(profile, CodecPreference.h264, EncodingQuality.balanced)
    high = resolve_rate_control(profile, CodecPreference.h264, EncodingQuality.high)

    assert (compact.average_bitrate, compact.peak_bitrate, compact.buffer_size) == (
        4_000_000,
        5_350_000,
        8_000_000,
    )
    assert (balanced.average_bitrate, balanced.peak_bitrate, balanced.buffer_size) == (
        6_000_000,
        8_000_000,
        12_000_000,
    )
    assert (high.average_bitrate, high.peak_bitrate, high.buffer_size) == (
        9_000_000,
        12_000_000,
        18_000_000,
    )


def test_h265_balanced_target_is_lower_than_h264_at_the_same_resolution() -> None:
    profile = PROFILES[QualityTarget.fhd_1080p]

    h264 = resolve_rate_control(profile, CodecPreference.h264, EncodingQuality.balanced)
    h265 = resolve_rate_control(profile, CodecPreference.h265, EncodingQuality.balanced)

    assert h265.average_bitrate < h264.average_bitrate
    assert h265.peak_bitrate < h264.peak_bitrate


def test_every_codec_and_resolution_has_monotonic_quality_tiers() -> None:
    for profile in PROFILES.values():
        for codec in (CodecPreference.h264, CodecPreference.h265):
            compact = resolve_rate_control(profile, codec, EncodingQuality.compact)
            balanced = resolve_rate_control(profile, codec, EncodingQuality.balanced)
            high = resolve_rate_control(profile, codec, EncodingQuality.high)

            assert compact.average_bitrate < balanced.average_bitrate < high.average_bitrate
            assert compact.peak_bitrate < balanced.peak_bitrate < high.peak_bitrate
            assert compact.buffer_size < balanced.buffer_size < high.buffer_size


def test_rkmpp_uses_explicit_constrained_vbr_for_h264(monkeypatch, tmp_path: Path) -> None:
    engine = TranscodeEngine.__new__(TranscodeEngine)
    engine.settings = Settings(_env_file=None)
    record = SimpleNamespace(
        source_width=1920,
        source_height=1080,
        media_duration_seconds=2.0,
        transcode_media_seconds=None,
        updated_at=None,
        cancel_requested=False,
        status=JobStatus.processing,
    )
    source = tmp_path / "source.mp4"
    destination = tmp_path / "output.mp4"
    source.write_bytes(b"source")
    captured: list[str] = []

    def fake_run(command, **_kwargs) -> None:
        captured.extend(command)
        destination.write_bytes(b"output")

    monkeypatch.setattr(engine, "_run_ffmpeg", fake_run)
    profile = PROFILES[QualityTarget.fhd_1080p]
    rate_control = resolve_rate_control(profile, CodecPreference.h264, EncodingQuality.balanced)

    engine._transcode_rkmpp(
        record,
        source,
        destination,
        profile,
        rate_control,
        "h264_rkmpp",
        "h264_rkmpp",
    )

    assert captured[captured.index("-rc_mode") + 1] == "0"
    assert captured[captured.index("-b:v") + 1] == "6000000"
    assert captured[captured.index("-maxrate") + 1] == "8000000"
    assert captured[captured.index("-bufsize") + 1] == "12000000"
    assert captured[captured.index("-profile:v") + 1] == "high"


def test_cpu_uses_the_same_rate_control_contract(monkeypatch, tmp_path: Path) -> None:
    engine = TranscodeEngine.__new__(TranscodeEngine)
    engine.settings = Settings(_env_file=None)
    record = SimpleNamespace(
        source_width=1920,
        source_height=1080,
        media_duration_seconds=2.0,
        transcode_media_seconds=None,
        updated_at=None,
        cancel_requested=False,
        status=JobStatus.processing,
    )
    source = tmp_path / "source.mp4"
    destination = tmp_path / "output.mp4"
    source.write_bytes(b"source")
    captured: list[str] = []

    def fake_run(command, **_kwargs) -> None:
        captured.extend(command)

    monkeypatch.setattr(engine, "_run_ffmpeg", fake_run)
    profile = PROFILES[QualityTarget.fhd_1080p]
    rate_control = resolve_rate_control(profile, CodecPreference.h265, EncodingQuality.high)

    engine._transcode_cpu(
        record,
        source,
        destination,
        profile,
        rate_control,
        CodecPreference.h265,
    )

    assert captured[captured.index("-c:v") + 1] == "libx265"
    assert captured[captured.index("-b:v") + 1] == "6750000"
    assert captured[captured.index("-maxrate") + 1] == "9000000"
    assert captured[captured.index("-bufsize") + 1] == "13500000"
