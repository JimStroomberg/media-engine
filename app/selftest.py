from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List

from .config import get_settings


@dataclass
class SelfTestResult:
    description: str
    passed: bool
    detail: str | None = None


class SelfTestFailure(RuntimeError):
    pass


def run_self_tests() -> List[SelfTestResult]:
    settings = get_settings()
    results: List[SelfTestResult] = []

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
            subprocess.run(test_cmd, check=True, capture_output=True)
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

    if settings.require_rk_accel:
        ffmpeg = shutil.which(settings.ffmpeg_command)
        if ffmpeg:
            try:
                decoders = subprocess.run([ffmpeg, '-hide_banner', '-loglevel', 'quiet', '-decoders'], check=True, capture_output=True, text=True).stdout.lower()
            except subprocess.CalledProcessError as exc:  # noqa: BLE001
                detail = (exc.stderr or b'').decode(errors='ignore')
                results.append(
                    SelfTestResult(
                        description='ffmpeg rk acceleration probe',
                        passed=False,
                        detail=detail or 'Unable to query ffmpeg decoders',
                    )
                )
            else:
                if 'rkmpp' not in decoders:
                    hint_parts = ['RKMPP decoders not detected in ffmpeg output.']
                    if Path('/dev/mpp_service').exists():
                        hint_parts.append('Host exposes /dev/mpp_service but container ffmpeg lacks RKMPP support.')
                    hint_parts.append('Install Rockchip multimedia ffmpeg (ppa:jjriek/rockchip-multimedia) or point MEDIA_ENGINE_FFMPEG_COMMAND to a hardware-enabled binary.')
                    results.append(
                        SelfTestResult(
                            description='ffmpeg rk acceleration probe',
                            passed=False,
                            detail=' '.join(hint_parts),
                        )
                    )
        else:
            results.append(
                SelfTestResult(
                    description='ffmpeg rk acceleration probe',
                    passed=False,
                    detail='ffmpeg binary not found',
                )
            )

    if any(not result.passed for result in results):
        failures = "; ".join(
            f"{result.description}: {result.detail or 'failed'}" for result in results if not result.passed
        )
        raise SelfTestFailure(failures)

    return results
