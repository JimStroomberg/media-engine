from __future__ import annotations

import ctypes.util
import os
import platform
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .config import Settings

JETSON_DECODERS = ("h264", "h265", "jpeg", "vp8", "vp9", "mpeg2", "mpeg4")
CODEC_ALIASES = {
    "avc": "h264",
    "avc1": "h264",
    "hevc": "h265",
    "hvc1": "h265",
    "mjpeg": "jpeg",
    "mjpegb": "jpeg",
    "mpeg2video": "mpeg2",
}


def normalize_video_codec(codec_name: str | None) -> str | None:
    """Return the stable capability name used for an ffprobe video codec."""

    if not codec_name:
        return None
    codec = codec_name.strip().lower()
    return CODEC_ALIASES.get(codec, codec) or None


def ffmpeg_video_decoders(settings: Settings) -> list[str]:
    """Return video codecs that the configured FFmpeg build can decode."""

    output = _query_ffmpeg(settings, "-codecs")
    decoders: set[str] = set()
    for line in output.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        flags = parts[0]
        if len(flags) < 3 or flags[0] != "D" or flags[2] != "V":
            continue
        if codec := normalize_video_codec(parts[1]):
            decoders.add(codec)
    return sorted(decoders)


def rkmpp_video_decoders(settings: Settings) -> list[str]:
    """Return codecs backed by an actual RKMPP decoder in the FFmpeg build."""

    output = _query_ffmpeg(settings, "-decoders")
    decoders: set[str] = set()
    for line in output.splitlines():
        parts = line.split()
        if len(parts) < 2 or not parts[1].lower().endswith("_rkmpp"):
            continue
        if codec := normalize_video_codec(parts[1].lower().removesuffix("_rkmpp")):
            decoders.add(codec)
    return sorted(decoders)


def _query_ffmpeg(settings: Settings, flag: str) -> str:
    ffmpeg = shutil.which(settings.ffmpeg_command)
    if ffmpeg is None:
        return ""
    try:
        return subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "quiet", flag],
            check=True,
            capture_output=True,
            text=True,
            timeout=settings.ffprobe_timeout_seconds,
        ).stdout
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return ""


def gstreamer_feature_available(settings: Settings, feature: str) -> bool:
    inspector = shutil.which(settings.gst_inspect_command)
    if inspector is None:
        return False
    try:
        subprocess.run(
            [inspector, feature],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=settings.ffprobe_timeout_seconds,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return False
    return True


def detected_worker_capabilities(settings: Settings) -> dict[str, list[str]]:
    processors: list[str] = []
    if shutil.which(settings.ffmpeg_command) and shutil.which(settings.ffprobe_command):
        processors.append("ffmpeg")
    if shutil.which(settings.tesseract_command):
        processors.append("tesseract")

    capabilities: dict[str, list[str]] = {
        "pipelines": ["transcode"],
        "backends": [settings.worker_backend],
        "encoders": ["h264", "h265"],
        "processors": processors,
        "providers": ["openai", "xai"],
    }

    if settings.worker_backend == "rkmpp":
        decoders = rkmpp_video_decoders(settings)
        if settings.allow_cpu_fallback:
            decoders = sorted(set(decoders) | set(ffmpeg_video_decoders(settings)))
        if decoders:
            capabilities["decoders"] = decoders
        capabilities["accelerators"] = ["rkmpp", "rga"]
        return capabilities

    if settings.worker_backend != "nvv4l2":
        decoders = ffmpeg_video_decoders(settings)
        if decoders:
            capabilities["decoders"] = decoders
        return capabilities

    processors.append("gstreamer")
    encoders = []
    if gstreamer_feature_available(settings, "nvv4l2h264enc"):
        encoders.append("h264")
    if gstreamer_feature_available(settings, "nvv4l2h265enc"):
        encoders.append("h265")
    capabilities["encoders"] = encoders

    accelerators = []
    if gstreamer_feature_available(settings, "nvv4l2decoder") and Path("/dev/nvhost-nvdec").exists():
        capabilities["decoders"] = list(JETSON_DECODERS)
        accelerators.append("nvdec")
    if settings.allow_cpu_fallback:
        capabilities["decoders"] = sorted(
            set(capabilities.get("decoders", [])) | set(ffmpeg_video_decoders(settings))
        )
    if encoders and Path("/dev/nvhost-msenc").exists():
        accelerators.append("nvenc")
    if gstreamer_feature_available(settings, "nvvidconv") and Path("/dev/nvhost-vic").exists():
        accelerators.append("vic")
        capabilities["video_transforms"] = ["scale", "colorspace", "composite"]
    if Path("/dev/nvhost-gpu").exists() and ctypes.util.find_library("cuda"):
        accelerators.append("cuda")
    if ctypes.util.find_library("nvinfer"):
        accelerators.append("tensorrt")
        if Path("/dev/nvhost-nvdla0").exists() and Path("/dev/nvhost-nvdla1").exists():
            accelerators.append("dla")
    capabilities["accelerators"] = accelerators

    if settings.worker_profile == "jetson-xavier-nx":
        hardware = ["jetson-xavier-nx", "volta-gpu", "dual-nvdla"]
        if Path("/dev/nvhost-pva0").exists() and Path("/dev/nvhost-pva1").exists():
            hardware.append("dual-pva")
        capabilities["hardware"] = hardware
    return capabilities


def detected_worker_runtime(settings: Settings, *, version: str) -> dict[str, Any]:
    runtime: dict[str, Any] = {
        "version": version,
        "hostname": platform.node(),
        "architecture": platform.machine(),
        "platform": platform.system().lower(),
        "profile": settings.worker_profile,
    }
    hardware_model = _read_first(
        Path("/host/proc/device-tree/model"),
        Path("/proc/device-tree/model"),
    )
    if hardware_model:
        runtime["hardware_model"] = hardware_model.rstrip("\x00")
    l4t_release = _read_first(
        Path("/host/etc/nv_tegra_release"),
        Path("/etc/nv_tegra_release"),
    )
    if l4t_release:
        match = re.search(r"R(?P<major>\d+) \(release\), REVISION: (?P<revision>[0-9.]+)", l4t_release)
        runtime["l4t_release"] = (
            f"R{match.group('major')}.{match.group('revision')}" if match else l4t_release.splitlines()[0]
        )
    if cuda_version := os.getenv("CUDA_VERSION"):
        runtime["cuda_version"] = cuda_version
    if tensorrt_version := os.getenv("NVIDIA_TENSORRT_VERSION"):
        runtime["tensorrt_version"] = tensorrt_version
    return runtime


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None


def _read_first(*paths: Path) -> str | None:
    for path in paths:
        if value := _read_text(path):
            return value
    return None
