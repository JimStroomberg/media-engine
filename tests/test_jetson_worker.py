from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app import hardware
from app.config import Settings
from app.models import CodecPreference, JobStatus, QualityTarget
from app.transcode import engine as engine_module
from app.transcode.engine import MediaCommandTimeout, TranscodeEngine, UnsupportedInputCodec
from app.transcode.probe import MediaInfo, MediaStreamInfo
from app.transcode.profiles import PROFILES


def jetson_settings() -> Settings:
    return Settings(
        _env_file=None,
        worker_backend="nvv4l2",
        worker_profile="jetson-xavier-nx",
        allow_cpu_fallback=False,
    )


def test_jetson_capabilities_report_only_detected_accelerators(monkeypatch) -> None:
    features = {
        "nvv4l2decoder",
        "nvv4l2h264enc",
        "nvv4l2h265enc",
        "nvvidconv",
    }
    devices = {
        "/dev/nvhost-nvdec",
        "/dev/nvhost-msenc",
        "/dev/nvhost-vic",
        "/dev/nvhost-gpu",
        "/dev/nvhost-nvdla0",
        "/dev/nvhost-nvdla1",
        "/dev/nvhost-pva0",
        "/dev/nvhost-pva1",
    }

    monkeypatch.setattr(hardware, "gstreamer_feature_available", lambda _settings, name: name in features)
    monkeypatch.setattr(hardware.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(hardware.ctypes.util, "find_library", lambda name: f"lib{name}.so")
    monkeypatch.setattr(Path, "exists", lambda path: str(path) in devices)

    capabilities = hardware.detected_worker_capabilities(jetson_settings())

    assert capabilities["backends"] == ["nvv4l2"]
    assert capabilities["encoders"] == ["h264", "h265"]
    assert capabilities["decoders"] == list(hardware.JETSON_DECODERS)
    assert capabilities["accelerators"] == ["nvdec", "nvenc", "vic", "cuda", "tensorrt", "dla"]
    assert capabilities["video_transforms"] == ["scale", "colorspace", "composite"]
    assert capabilities["hardware"] == ["jetson-xavier-nx", "volta-gpu", "dual-nvdla", "dual-pva"]
    assert "gstreamer" in capabilities["processors"]


def test_jetson_capabilities_do_not_claim_missing_devices(monkeypatch) -> None:
    monkeypatch.setattr(hardware, "gstreamer_feature_available", lambda _settings, _name: True)
    monkeypatch.setattr(hardware.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(hardware.ctypes.util, "find_library", lambda _name: None)
    monkeypatch.setattr(Path, "exists", lambda _path: False)

    capabilities = hardware.detected_worker_capabilities(jetson_settings())

    assert capabilities["encoders"] == ["h264", "h265"]
    assert capabilities["accelerators"] == []
    assert "decoders" not in capabilities
    assert capabilities["hardware"] == ["jetson-xavier-nx", "volta-gpu", "dual-nvdla"]


def test_jetson_runtime_reports_l4t_cuda_and_tensorrt(monkeypatch) -> None:
    def fake_read(path: Path) -> str | None:
        if str(path) == "/proc/device-tree/model":
            return "NVIDIA Jetson Xavier NX Developer Kit\x00"
        if str(path) == "/etc/nv_tegra_release":
            return "# R35 (release), REVISION: 6.5, GCID: 123"
        return None

    monkeypatch.setattr(hardware, "_read_text", fake_read)
    monkeypatch.setenv("CUDA_VERSION", "11.4.19")
    monkeypatch.setenv("NVIDIA_TENSORRT_VERSION", "8.5.2")

    runtime = hardware.detected_worker_runtime(jetson_settings(), version="0.4.0")

    assert runtime["hardware_model"] == "NVIDIA Jetson Xavier NX Developer Kit"
    assert runtime["l4t_release"] == "R35.6.5"
    assert runtime["cuda_version"] == "11.4.19"
    assert runtime["tensorrt_version"] == "8.5.2"


def test_jetson_codec_selection_normalizes_mjpeg(monkeypatch) -> None:
    monkeypatch.setattr(engine_module, "gstreamer_feature_available", lambda _settings, _name: True)
    transcode_engine = TranscodeEngine.__new__(TranscodeEngine)
    transcode_engine.settings = jetson_settings()
    media = MediaInfo(
        container="avi",
        bit_rate=None,
        duration=1.0,
        video=MediaStreamInfo(codec_type="video", codec_name="mjpeg", width=640, height=480),
        audio=None,
    )

    use_hardware, decoder, encoder = transcode_engine._select_jetson_codecs(media, CodecPreference.h265)

    assert use_hardware is True
    assert decoder == "nvv4l2decoder"
    assert encoder == "nvv4l2h265enc"


def test_video_codec_capabilities_are_normalized_from_ffmpeg(monkeypatch) -> None:
    output = """
 DEV.LS h264 H.264
 DEV.L. hevc H.265
 D.V.L. av1 Alliance for Open Media AV1
 DEA.L. aac AAC audio
"""
    monkeypatch.setattr(hardware.shutil, "which", lambda _name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(hardware.subprocess, "run", lambda *_args, **_kwargs: SimpleNamespace(stdout=output))

    assert hardware.ffmpeg_video_decoders(Settings(_env_file=None)) == ["av1", "h264", "h265"]


def test_rkmpp_capabilities_report_only_present_hardware_decoders(monkeypatch) -> None:
    output = """
 V..... av1_rkmpp Rockchip AV1 decoder
 V..... h264_rkmpp Rockchip H.264 decoder
 V..... hevc_rkmpp Rockchip HEVC decoder
 V..... vp9 Generic VP9 decoder
"""
    monkeypatch.setattr(hardware.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(hardware.subprocess, "run", lambda *_args, **_kwargs: SimpleNamespace(stdout=output))

    capabilities = hardware.detected_worker_capabilities(
        Settings(_env_file=None, worker_backend="rkmpp", allow_cpu_fallback=False)
    )

    assert capabilities["decoders"] == ["av1", "h264", "h265"]


def test_jetson_rejects_unsupported_input_before_pipeline_start() -> None:
    transcode_engine = TranscodeEngine.__new__(TranscodeEngine)
    transcode_engine.settings = jetson_settings()
    media = MediaInfo(
        container="matroska",
        bit_rate=None,
        duration=1.0,
        video=MediaStreamInfo(codec_type="video", codec_name="av1", width=1920, height=1080),
        audio=None,
    )

    with pytest.raises(UnsupportedInputCodec, match="unsupported_input_codec.*av1"):
        transcode_engine._validate_backend_input_codec(media)


def test_jetson_pipeline_converts_compositor_rgba_output_for_nvenc(monkeypatch, tmp_path) -> None:
    transcode_engine = TranscodeEngine.__new__(TranscodeEngine)
    transcode_engine.settings = jetson_settings()
    source = tmp_path / "source.mp4"
    destination = tmp_path / "output.mp4"
    source.write_bytes(b"source")
    record = SimpleNamespace(
        source_width=1280,
        source_height=720,
        media_duration_seconds=3.0,
        transcode_media_seconds=None,
        updated_at=None,
        cancel_requested=False,
        status=JobStatus.processing,
    )
    captured: dict[str, list[str]] = {}

    def fake_gstreamer(command, **_kwargs) -> None:
        captured["gstreamer"] = command
        video_path = Path(next(part.split("=", 1)[1] for part in command if part.startswith("location=")))
        video_path.write_bytes(b"video")

    def fake_ffmpeg(command, **_kwargs) -> None:
        captured["ffmpeg"] = command
        Path(command[-1]).write_bytes(b"muxed")

    monkeypatch.setattr(transcode_engine, "_run_gstreamer", fake_gstreamer)
    monkeypatch.setattr(transcode_engine, "_run_ffmpeg", fake_ffmpeg)

    transcode_engine._transcode_jetson(
        record,
        source,
        destination,
        PROFILES[QualityTarget.sd_480p],
        CodecPreference.h265,
    )

    command = captured["gstreamer"]
    assert command[command.index("watchdog") + 1] == "timeout=120000"
    rgba_index = command.index("video/x-raw(memory:NVMM),format=RGBA,width=848,height=480")
    nv12_index = command.index("video/x-raw(memory:NVMM),format=NV12,width=848,height=480")
    assert command[rgba_index + 2] == "nvvidconv"
    assert rgba_index < nv12_index < command.index("nvv4l2h265enc")
    assert "hvc1" in captured["ffmpeg"]
    assert destination.read_bytes() == b"muxed"


def test_gstreamer_hard_timeout_terminates_the_process(monkeypatch) -> None:
    process = Mock()
    process.poll.side_effect = lambda: 0 if process.terminate.called else None
    process.stdout = Mock()
    process.stdout.close.return_value = None
    process.wait.return_value = 0
    transcode_engine = TranscodeEngine.__new__(TranscodeEngine)
    transcode_engine.settings = Settings(
        _env_file=None,
        media_command_timeout_seconds=30,
        media_no_progress_timeout_seconds=15,
    )
    monotonic = iter([0.0, 31.0])
    monkeypatch.setattr(engine_module.subprocess, "Popen", lambda *_args, **_kwargs: process)
    monkeypatch.setattr(engine_module.time, "monotonic", lambda: next(monotonic))

    with pytest.raises(MediaCommandTimeout, match="media_command_timeout"):
        transcode_engine._run_gstreamer(["gst-launch-1.0"], action="test", env={})

    process.terminate.assert_called_once()


def test_ffmpeg_no_progress_timeout_terminates_the_process(monkeypatch) -> None:
    process = Mock()
    process.poll.side_effect = lambda: 0 if process.terminate.called else None
    process.stdout = Mock()
    process.stdout.close.return_value = None
    process.wait.return_value = 0
    transcode_engine = TranscodeEngine.__new__(TranscodeEngine)
    transcode_engine.settings = Settings(
        _env_file=None,
        media_command_timeout_seconds=60,
        media_no_progress_timeout_seconds=15,
    )
    monotonic = iter([0.0, 0.0, 16.0])
    monkeypatch.setattr(engine_module.subprocess, "Popen", lambda *_args, **_kwargs: process)
    monkeypatch.setattr(engine_module.select, "select", lambda *_args, **_kwargs: ([], [], []))
    monkeypatch.setattr(engine_module.time, "monotonic", lambda: next(monotonic))

    with pytest.raises(MediaCommandTimeout, match="media_no_progress_timeout"):
        transcode_engine._run_ffmpeg(["ffmpeg"], action="test")

    process.terminate.assert_called_once()
