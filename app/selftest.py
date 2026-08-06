from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .config import get_settings
from .hardware import gstreamer_feature_available


@dataclass
class SelfTestResult:
    description: str
    passed: bool
    detail: str | None = None


class SelfTestFailure(RuntimeError):
    pass


def run_self_tests() -> list[SelfTestResult]:
    settings = get_settings()
    results: list[SelfTestResult] = []

    for binary in (settings.ffmpeg_command, settings.ffprobe_command):
        if shutil.which(binary) is None:
            results.append(
                SelfTestResult(
                    description=f"Binary '{binary}' available",
                    passed=False,
                    detail=f"{binary} not found",
                )
            )
        else:
            results.append(SelfTestResult(description=f"Binary '{binary}' available", passed=True))

    ffmpeg = shutil.which(settings.ffmpeg_command)
    if ffmpeg:
        test_cmd = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x240:rate=15",
            "-t",
            "1",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-f",
            "null",
            "-",
        ]
        try:
            subprocess.run(test_cmd, check=True, capture_output=True, timeout=settings.ffprobe_timeout_seconds)
            results.append(SelfTestResult(description="ffmpeg test pattern encode", passed=True))
        except subprocess.CalledProcessError as exc:  # noqa: BLE001
            detail = (exc.stderr or b"").decode(errors="ignore")
            results.append(
                SelfTestResult(
                    description="ffmpeg test pattern encode",
                    passed=False,
                    detail=detail or "ffmpeg encode test failed",
                )
            )
        except subprocess.TimeoutExpired:
            results.append(
                SelfTestResult(
                    description="ffmpeg test pattern encode",
                    passed=False,
                    detail=f"ffmpeg encode test exceeded {settings.ffprobe_timeout_seconds}s timeout",
                )
            )

    if settings.require_rk_accel:
        ffmpeg = shutil.which(settings.ffmpeg_command)
        if ffmpeg:
            try:
                decoders = subprocess.run(
                    [ffmpeg, "-hide_banner", "-loglevel", "quiet", "-decoders"],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=settings.ffprobe_timeout_seconds,
                ).stdout.lower()
            except subprocess.CalledProcessError as exc:  # noqa: BLE001
                detail = (exc.stderr or b"").decode(errors="ignore")
                results.append(
                    SelfTestResult(
                        description="ffmpeg rk acceleration probe",
                        passed=False,
                        detail=detail or "Unable to query ffmpeg decoders",
                    )
                )
            except subprocess.TimeoutExpired:
                results.append(
                    SelfTestResult(
                        description="ffmpeg rk acceleration probe",
                        passed=False,
                        detail=f"ffmpeg decoder query exceeded {settings.ffprobe_timeout_seconds}s timeout",
                    )
                )
            else:
                if "rkmpp" not in decoders:
                    hint_parts = ["RKMPP decoders not detected in ffmpeg output."]
                    if Path("/dev/mpp_service").exists():
                        hint_parts.append("Host exposes /dev/mpp_service but container ffmpeg lacks RKMPP support.")
                    hint_parts.append(
                        "Install Rockchip multimedia ffmpeg (ppa:jjriek/rockchip-multimedia) or "
                        "point MEDIA_ENGINE_FFMPEG_COMMAND to a hardware-enabled binary."
                    )
                    results.append(
                        SelfTestResult(
                            description="ffmpeg rk acceleration probe",
                            passed=False,
                            detail=" ".join(hint_parts),
                        )
                    )
        else:
            results.append(
                SelfTestResult(
                    description="ffmpeg rk acceleration probe",
                    passed=False,
                    detail="ffmpeg binary not found",
                )
            )

    if settings.require_jetson_accel:
        required_features = (
            "nvv4l2decoder",
            "nvv4l2h264enc",
            "nvv4l2h265enc",
            "nvvidconv",
            "nvcompositor",
            "h264parse",
            "h265parse",
            "qtmux",
        )
        missing_features = [
            feature for feature in required_features if not gstreamer_feature_available(settings, feature)
        ]
        required_devices = (
            Path("/dev/nvhost-nvdec"),
            Path("/dev/nvhost-msenc"),
            Path("/dev/nvhost-vic"),
            Path("/dev/nvmap"),
        )
        missing_devices = [str(path) for path in required_devices if not path.exists()]
        if missing_features or missing_devices:
            detail_parts = []
            if missing_features:
                detail_parts.append(f"missing GStreamer features: {', '.join(missing_features)}")
            if missing_devices:
                detail_parts.append(f"missing devices: {', '.join(missing_devices)}")
            results.append(
                SelfTestResult(
                    description="Jetson V4L2 acceleration probe",
                    passed=False,
                    detail="; ".join(detail_parts),
                )
            )
        else:
            gstreamer = shutil.which(settings.gstreamer_command)
            if gstreamer is None:
                results.append(
                    SelfTestResult(
                        description="Jetson V4L2 acceleration probe",
                        passed=False,
                        detail=f"{settings.gstreamer_command} not found",
                    )
                )
            else:
                environment = os.environ.copy()
                environment["GST_PLUGIN_FEATURE_RANK"] = "nvv4l2decoder:MAX"
                phase = "encode"
                with tempfile.TemporaryDirectory(prefix="jetson-selftest-", dir=settings.temp_dir) as temp_dir:
                    probe_file = Path(temp_dir) / "probe.h264"
                    encode_cmd = [
                        gstreamer,
                        "-q",
                        "-e",
                        "videotestsrc",
                        "num-buffers=30",
                        "!",
                        "video/x-raw,width=320,height=240,framerate=15/1",
                        "!",
                        "nvvidconv",
                        "!",
                        "video/x-raw(memory:NVMM),format=NV12",
                        "!",
                        "nvv4l2h264enc",
                        "maxperf-enable=true",
                        "bitrate=1000000",
                        "insert-sps-pps=true",
                        "!",
                        "h264parse",
                        "config-interval=-1",
                        "!",
                        "filesink",
                        f"location={probe_file}",
                    ]
                    decode_cmd = [
                        gstreamer,
                        "-q",
                        "filesrc",
                        f"location={probe_file}",
                        "!",
                        "h264parse",
                        "!",
                        "nvv4l2decoder",
                        "!",
                        "fakesink",
                        "sync=false",
                    ]
                    try:
                        subprocess.run(
                            encode_cmd,
                            check=True,
                            capture_output=True,
                            timeout=settings.ffprobe_timeout_seconds,
                            env=environment,
                        )
                        if not probe_file.exists() or probe_file.stat().st_size == 0:
                            raise subprocess.CalledProcessError(
                                1, encode_cmd, stderr=b"hardware encoder produced no data"
                            )
                        phase = "decode"
                        subprocess.run(
                            decode_cmd,
                            check=True,
                            capture_output=True,
                            timeout=settings.ffprobe_timeout_seconds,
                            env=environment,
                        )
                        results.append(SelfTestResult(description="Jetson V4L2 acceleration probe", passed=True))
                    except subprocess.CalledProcessError as exc:
                        detail = (exc.stderr or b"").decode(errors="ignore")
                        results.append(
                            SelfTestResult(
                                description="Jetson V4L2 acceleration probe",
                                passed=False,
                                detail=detail or f"Jetson hardware {phase} test failed",
                            )
                        )
                    except subprocess.TimeoutExpired:
                        results.append(
                            SelfTestResult(
                                description="Jetson V4L2 acceleration probe",
                                passed=False,
                                detail=(
                                    f"Jetson hardware {phase} test exceeded {settings.ffprobe_timeout_seconds}s timeout"
                                ),
                            )
                        )

    if any(not result.passed for result in results):
        failures = "; ".join(
            f"{result.description}: {result.detail or 'failed'}" for result in results if not result.passed
        )
        raise SelfTestFailure(failures)

    return results
