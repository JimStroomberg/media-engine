from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
from datetime import datetime
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, List, Optional, Tuple

from ..config import get_settings
from ..models import CodecPreference, JobRequest
from ..transcode.probe import MediaInfo, ProbeError, probe_media
from ..transcode.profiles import QualityProfile, choose_profile

logger = logging.getLogger(__name__)


@dataclass
class TranscodeResult:
    output_path: Path
    remuxed: bool
    profile: QualityProfile
    codec: CodecPreference


class TranscodeEngine:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.ffmpeg_decoders = self._query_ffmpeg_list("decoders")
        self.ffmpeg_encoders = self._query_ffmpeg_list("encoders")

    async def process(self, record, request: JobRequest) -> TranscodeResult:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._process_sync, record, request)

    def _process_sync(self, record, request: JobRequest) -> TranscodeResult:
        source_path = record.source_path
        job_id = record.job_id
        logger.info("Transcode started", extra={"job_id": job_id, "source_path": str(source_path)})
        info = self._probe(source_path)
        video_info = info.video if info.video else None
        logger.info(
            "Probe summary",
            extra={
                "job_id": job_id,
                "container": info.container,
                "video_codec": getattr(video_info, "codec_name", None),
                "video_width": getattr(video_info, "width", None),
                "video_height": getattr(video_info, "height", None),
            },
        )
        profile = self._select_profile(info, request)
        target_codec = self._resolve_codec(info, profile, request)
        logger.info(
            "Profile resolved",
            extra={
                "job_id": job_id,
                "profile": profile.name.value,
                "target_codec": target_codec.value,
                "requested_quality": request.quality.value,
                "requested_codec": request.codec.value,
            },
        )

        output_path = self.settings.output_dir / f"{job_id}.mp4"
        work_output = self.settings.work_dir / f"{job_id}.mp4"
        work_output.parent.mkdir(parents=True, exist_ok=True)

        record.media_duration_seconds = info.duration

        if self._should_remux(info, profile, target_codec):
            logger.info("Selected remux path", extra={"job_id": job_id})
            self._remux(source_path, work_output)
            remuxed = True
        else:
            use_hw, decoder, encoder = self._select_rkmpp_codecs(info, target_codec)
            logger.info(
                "Selected transcode path",
                extra={
                    "job_id": job_id,
                    "profile": profile.name.value,
                    "codec": target_codec.value,
                    "rkmpp_encoder": use_hw,
                    "rkmpp_decoder": bool(decoder),
                },
            )
            if use_hw:
                self._transcode_rkmpp(record, source_path, work_output, profile, decoder, encoder)
            else:
                self._transcode_cpu(record, source_path, work_output, profile, target_codec)
            remuxed = False

        shutil.move(work_output, output_path)
        if record.media_duration_seconds is not None:
            record.transcode_media_seconds = record.media_duration_seconds
        logger.info(
            "Transcode finished",
            extra={"job_id": job_id, "output_path": str(output_path), "remuxed": remuxed},
        )
        return TranscodeResult(output_path=output_path, remuxed=remuxed, profile=profile, codec=target_codec)

    def _probe(self, source_path: Path) -> MediaInfo:
        try:
            info = probe_media(source_path)
            logger.debug("Probe result", extra={"info": info})
            return info
        except ProbeError as exc:
            logger.error("Probe failed", extra={"error": str(exc)})
            raise

    def _select_profile(self, info: MediaInfo, request: JobRequest) -> QualityProfile:
        video = info.video
        if not video or not video.width or not video.height:
            raise RuntimeError("Unable to determine source resolution")
        return choose_profile(video.width, video.height, request.quality)

    def _resolve_codec(
        self,
        info: MediaInfo,
        profile: QualityProfile,
        request: JobRequest,
    ) -> CodecPreference:
        if request.codec != CodecPreference.auto:
            return request.codec

        source_codec = self._map_codec_name(info.video.codec_name if info.video else None)
        if source_codec in {CodecPreference.h264, CodecPreference.h265}:
            return source_codec

        if profile.codec != CodecPreference.auto:
            return profile.codec

        return CodecPreference.h264

    def _should_remux(
        self,
        info: MediaInfo,
        profile: QualityProfile,
        target_codec: CodecPreference,
    ) -> bool:
        video = info.video
        if not video or not video.width or not video.height:
            return False
        source_codec = self._map_codec_name(video.codec_name)
        if target_codec == CodecPreference.auto:
            target_codec = source_codec or CodecPreference.h264
        if source_codec != target_codec:
            return False

        is_mp4_container = bool(info.container and "mp4" in info.container.lower())
        if not is_mp4_container:
            return False

        if video.width > profile.width or video.height > profile.height:
            return False

        return True

    def _remux(self, source_path: Path, dest_path: Path) -> None:
        cmd = [
            self.settings.ffmpeg_command,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source_path),
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            str(dest_path),
        ]
        self._run_ffmpeg(cmd, action="remux")

    def _transcode_cpu(
        self,
        record,
        source_path: Path,
        dest_path: Path,
        profile: QualityProfile,
        codec: CodecPreference,
    ) -> None:
        video_codec = "libx265" if codec == CodecPreference.h265 else "libx264"
        bitrate = str(profile.video_bitrate)
        vf = (
            f"scale=w={profile.width}:h={profile.height}:force_original_aspect_ratio=decrease"
            f",pad=w={profile.width}:h={profile.height}:x=(ow-iw)/2:y=(oh-ih)/2"
        )
        cmd = [
            self.settings.ffmpeg_command,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source_path),
            "-vf",
            vf,
            "-c:v",
            video_codec,
            "-b:v",
            bitrate,
            "-preset",
            "veryfast",
            "-movflags",
            "+faststart",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            str(dest_path),
        ]
        duration = record.media_duration_seconds or 0.0
        self._run_ffmpeg(
            cmd,
            action="transcode",
            progress_handler=lambda seconds: self._update_progress(record, seconds, duration),
        )

    def _transcode_rkmpp(
        self,
        record,
        source_path: Path,
        dest_path: Path,
        profile: QualityProfile,
        decoder_name: str,
        encoder_name: str,
    ) -> None:
        filter_candidates: list[tuple[str, str]] = []
        if Path("/dev/rga").exists():
            filter_candidates.append(("rkrga", "rkrga=fmt=nv12,pad=ceil(iw/16)*16:ceil(ih/16)*16"))
        filter_candidates.append(("format", "format=nv12,pad=ceil(iw/16)*16:ceil(ih/16)*16"))

        decoder_args: list[str] = []
        if decoder_name:
            decoder_args = ["-hwaccel", "rkmpp", "-c:v", decoder_name]

        base_cmd = [
            self.settings.ffmpeg_command,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            *decoder_args,
            "-i",
            str(source_path),
        ]

        last_error: RuntimeError | None = None
        for label, filter_expr in filter_candidates:
            cmd = list(base_cmd)
            cmd.extend(["-vf", filter_expr])
            cmd.extend(["-c:v", encoder_name])
            cmd.extend(["-pix_fmt", "nv12"])
            if encoder_name == "hevc_rkmpp":
                cmd.extend(["-profile:v", "main", "-tag:v", "hvc1"])
                bv, maxrate, bufsize = self._hevc_rate_control(profile.width)
                cmd.extend(["-b:v", bv, "-maxrate", maxrate, "-bufsize", bufsize])
            else:
                cmd.extend(["-b:v", str(profile.video_bitrate)])
            cmd.extend([
                "-g",
                "240",
                "-movflags",
                "+faststart",
                "-c:a",
                "aac",
                "-b:a",
                "128k",
                str(dest_path),
            ])
            logger.info(
                "Running RKMPP ffmpeg",
                extra={"mode": label, "command": " ".join(cmd[:12]) + " ..."},
            )
            try:
                duration = record.media_duration_seconds or 0.0
                self._run_ffmpeg(
                    cmd,
                    action=f"transcode-rkmpp-{label}",
                    progress_handler=lambda seconds: self._update_progress(record, seconds, duration),
                )
                return
            except RuntimeError as exc:
                last_error = exc
                try:
                    Path(dest_path).unlink()
                except FileNotFoundError:
                    pass

        if last_error:
            raise last_error
        raise RuntimeError("rk transcode failed")

    def _run_ffmpeg(
        self,
        cmd: list[str],
        action: str,
        env: dict[str, str] | None = None,
        progress_handler: Optional[Callable[[float], None]] = None,
    ) -> None:
        logger.info("Running ffmpeg", extra={"action": action, "command": " ".join(cmd)})
        wrapped_cmd = [cmd[0], "-progress", "pipe:1", "-nostats"] + cmd[1:]
        process = subprocess.Popen(
            wrapped_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        stderr_data = ""
        try:
            if process.stdout:
                for line in process.stdout:
                    if progress_handler and line.startswith("out_time_ms="):
                        try:
                            microseconds = int(line.strip().split("=", 1)[1])
                            seconds = microseconds / 1_000_000
                            progress_handler(seconds)
                        except ValueError:
                            continue
            if process.stderr:
                stderr_data = process.stderr.read()
            return_code = process.wait()
        finally:
            if process.stdout:
                process.stdout.close()
            if process.stderr:
                process.stderr.close()

        if return_code != 0:
            logger.error(
                "ffmpeg command failed",
                extra={"action": action, "stderr": stderr_data},
            )
            raise RuntimeError(f"ffmpeg {action} failed")

    def _query_ffmpeg_list(self, list_type: str) -> set[str]:
        ffmpeg = shutil.which(self.settings.ffmpeg_command)
        if not ffmpeg:
            return set()
        flag = f"-{list_type}"
        try:
            result = subprocess.run(
                [ffmpeg, "-hide_banner", "-loglevel", "quiet", flag],
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError:
            return set()
        names: set[str] = set()
        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[1].isidentifier():
                names.add(parts[1].lower())
        return names

    def _select_rkmpp_codecs(
        self,
        info: MediaInfo,
        target_codec: CodecPreference,
    ) -> tuple[bool, str, str]:
        decoder_name = None
        if info.video and info.video.codec_name:
            decoder_name = self._rkmpp_decoder_name(info.video.codec_name)
            if decoder_name and decoder_name.lower() not in self.ffmpeg_decoders:
                decoder_name = None
        encoder_name = self._rkmpp_encoder_name(target_codec)
        if encoder_name and encoder_name.lower() not in self.ffmpeg_encoders:
            encoder_name = None
        use_hw = bool(encoder_name)
        return use_hw, decoder_name or "", encoder_name or ""

    def _rkmpp_decoder_name(self, codec_name: str) -> str | None:
        codec = codec_name.lower()
        mapping = {
            "av1": "av1_rkmpp",
            "vp9": "vp9_rkmpp",
            "vp8": "vp8_rkmpp",
            "h264": "h264_rkmpp",
            "avc1": "h264_rkmpp",
            "avc": "h264_rkmpp",
            "h265": "hevc_rkmpp",
            "hevc": "hevc_rkmpp",
        }
        return mapping.get(codec)

    def _rkmpp_encoder_name(self, target_codec: CodecPreference) -> str | None:
        if target_codec == CodecPreference.h265:
            return "hevc_rkmpp"
        return "h264_rkmpp"

    def _pick_hw_device(self) -> str | None:
        candidates = [
            "/dev/dri/renderD128",
            "/dev/dri/renderD129",
            "/dev/dri/card0",
        ]
        for device in candidates:
            if Path(device).exists():
                return device
        return None

    def _hevc_rate_control(self, width: int) -> tuple[str, str, str]:
        if width >= 3800:
            return "8M", "12M", "18M"
        if width >= 2500:
            return "5M", "8M", "12M"
        return "3M", "5M", "8M"

    def _update_progress(self, record, processed_seconds: float, media_duration: float) -> None:
        record.transcode_media_seconds = processed_seconds
        if record.media_duration_seconds is None and media_duration > 0:
            record.media_duration_seconds = media_duration
        record.updated_at = datetime.utcnow()

    def _map_codec_name(self, codec: str | None) -> CodecPreference | None:
        if not codec:
            return None
        value = codec.lower()
        if value in {"h264", "avc1", "avc"}:
            return CodecPreference.h264
        if value in {"h265", "hevc", "hvc1"}:
            return CodecPreference.h265
        return None
