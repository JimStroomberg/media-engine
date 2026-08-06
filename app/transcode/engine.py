from __future__ import annotations

import asyncio
import logging
import os
import select
import shutil
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ..config import get_settings
from ..hardware import JETSON_DECODERS, gstreamer_feature_available, normalize_video_codec
from ..models import CodecPreference, JobRequest, JobStatus, QualityTarget
from ..transcode.probe import MediaInfo, ProbeError, probe_media
from ..transcode.profiles import QualityProfile, choose_profile

logger = logging.getLogger(__name__)


@dataclass
class TranscodeResult:
    output_path: Path
    remuxed: bool
    profile: QualityProfile | None
    codec: CodecPreference


class TranscodeCancelled(RuntimeError):
    pass


class MediaCommandTimeout(RuntimeError):
    pass


class UnsupportedInputCodec(RuntimeError):
    def __init__(self, codec: str, backend: str) -> None:
        self.codec = codec
        self.backend = backend
        super().__init__(f"unsupported_input_codec: {backend} cannot decode input codec '{codec}'")


class TranscodeEngine:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.ffmpeg_decoders = self._query_ffmpeg_list("decoders")
        self.ffmpeg_encoders = self._query_ffmpeg_list("encoders")

    async def process(self, record, request: JobRequest) -> TranscodeResult:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._process_sync, record, request)

    def input_codec(self, source_path: Path) -> str | None:
        """Probe and normalize the source video codec for worker routing."""

        info = self._probe(source_path)
        return normalize_video_codec(info.video.codec_name if info.video else None)

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
        record.media_duration_seconds = info.duration

        if request.quality == QualityTarget.audio_only:
            return self._process_audio_only(record, info, source_path, job_id)

        profile = self._select_profile(info, request)
        if request.quality != QualityTarget.auto and profile.name != request.quality:
            logger.info(
                "Requested quality downgraded to fit source",
                extra={
                    "job_id": job_id,
                    "requested_quality": request.quality.value,
                    "resolved_quality": profile.name.value,
                    "source_width": getattr(video_info, "width", None),
                    "source_height": getattr(video_info, "height", None),
                },
            )
        record.quality = profile.name
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

        if info.video:
            record.source_width = info.video.width or record.source_width
            record.source_height = info.video.height or record.source_height

        if self._should_remux(info, profile, target_codec):
            logger.info("Selected remux path", extra={"job_id": job_id})
            self._remux(record, source_path, work_output)
            remuxed = True
        else:
            self._validate_backend_input_codec(info)
            hardware_backend, decoder, encoder = self._select_hardware_codecs(info, target_codec)
            if self.settings.require_jetson_accel and self.settings.worker_backend == "nvv4l2" and not hardware_backend:
                raise RuntimeError(f"Required Jetson hardware encoder is unavailable for codec {target_codec.value}")
            logger.info(
                "Selected transcode path",
                extra={
                    "job_id": job_id,
                    "profile": profile.name.value,
                    "codec": target_codec.value,
                    "hardware_backend": hardware_backend,
                    "hardware_encoder": encoder or None,
                    "hardware_decoder": decoder or None,
                },
            )
            if hardware_backend:
                allow_cpu_fallback = self.settings.allow_cpu_fallback
                try:
                    if hardware_backend == "rkmpp":
                        self._transcode_rkmpp(record, source_path, work_output, profile, decoder, encoder)
                    elif hardware_backend == "nvv4l2":
                        self._transcode_jetson(record, source_path, work_output, profile, target_codec)
                    else:
                        raise RuntimeError(f"Unsupported hardware backend: {hardware_backend}")
                except RuntimeError as exc:
                    if allow_cpu_fallback:
                        logger.warning(
                            "Hardware transcode failed, falling back to CPU",
                            extra={
                                "job_id": job_id,
                                "profile": profile.name.value,
                                "codec": target_codec.value,
                                "error": str(exc),
                            },
                        )
                        try:
                            work_output.unlink()
                        except FileNotFoundError:
                            pass
                        self._transcode_cpu(record, source_path, work_output, profile, target_codec)
                    else:
                        logger.error(
                            "Hardware transcode failed and CPU fallback disabled",
                            extra={
                                "job_id": job_id,
                                "profile": profile.name.value,
                                "codec": target_codec.value,
                                "error": str(exc),
                            },
                        )
                        raise
                else:
                    matches_profile, measured_dims = self._output_matches_profile(work_output, profile)
                    if not matches_profile:
                        width, height = measured_dims if measured_dims else (None, None)
                        if allow_cpu_fallback:
                            logger.warning(
                                "Hardware transcode output exceeds requested profile, rerunning on CPU",
                                extra={
                                    "job_id": job_id,
                                    "profile": profile.name.value,
                                    "target_width": profile.width,
                                    "target_height": profile.height,
                                    "output_width": width,
                                    "output_height": height,
                                },
                            )
                            try:
                                work_output.unlink()
                            except FileNotFoundError:
                                pass
                            self._transcode_cpu(record, source_path, work_output, profile, target_codec)
                        else:
                            logger.error(
                                "Hardware transcode output exceeds requested profile and CPU fallback disabled",
                                extra={
                                    "job_id": job_id,
                                    "profile": profile.name.value,
                                    "target_width": profile.width,
                                    "target_height": profile.height,
                                    "output_width": width,
                                    "output_height": height,
                                },
                            )
                            try:
                                work_output.unlink()
                            except FileNotFoundError:
                                pass
                            raise RuntimeError("Hardware output exceeds requested profile bounds")
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

    def _process_audio_only(
        self,
        record,
        info: MediaInfo,
        source_path: Path,
        job_id: str,
    ) -> TranscodeResult:
        if not info.audio:
            raise RuntimeError("No audio stream available for extraction")

        output_path = self.settings.output_dir / f"{job_id}.m4a"
        work_output = self.settings.work_dir / f"{job_id}.m4a"
        work_output.parent.mkdir(parents=True, exist_ok=True)

        self._transcode_audio(record, source_path, work_output)
        shutil.move(work_output, output_path)
        if record.media_duration_seconds is not None:
            record.transcode_media_seconds = record.media_duration_seconds
        logger.info(
            "Audio extraction finished",
            extra={"job_id": job_id, "output_path": str(output_path)},
        )
        return TranscodeResult(output_path=output_path, remuxed=False, profile=None, codec=CodecPreference.auto)

    def _remux(self, record, source_path: Path, dest_path: Path) -> None:
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
        self._run_ffmpeg(cmd, action="remux", should_cancel=lambda: self._is_cancelled(record))

    def _transcode_audio(
        self,
        record,
        source_path: Path,
        dest_path: Path,
    ) -> None:
        cmd = [
            self.settings.ffmpeg_command,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source_path),
            "-vn",
            "-acodec",
            "aac",
            "-b:a",
            "192k",
            "-movflags",
            "+faststart",
            str(dest_path),
        ]
        duration = record.media_duration_seconds or 0.0
        self._run_ffmpeg(
            cmd,
            action="extract-audio",
            progress_handler=lambda seconds: self._update_progress(record, seconds, duration),
            should_cancel=lambda: self._is_cancelled(record),
        )

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
        scaled_w, scaled_h = self._compute_scaled_dimensions(record, profile)
        vf = f"scale={scaled_w}:{scaled_h}"
        pad = f"pad={profile.width}:{profile.height}:(ow-iw)/2:(oh-ih)/2"
        cmd = [
            self.settings.ffmpeg_command,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source_path),
            "-vf",
            f"{vf},{pad}",
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
            should_cancel=lambda: self._is_cancelled(record),
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
        scaled_w, scaled_h = self._compute_scaled_dimensions(record, profile)
        target_w, target_h = profile.width, profile.height

        filter_candidates: list[tuple[str, str]] = []
        # Keep a broadly compatible RKMPP path: this filter chain works across
        # current RK1 ffmpeg packages where scale_rkrga format negotiation may fail.
        fallback_filter = f"scale={scaled_w}:{scaled_h},format=nv12"
        if target_w != scaled_w or target_h != scaled_h:
            fallback_filter += f",pad={target_w}:{target_h}:(ow-iw)/2:(oh-ih)/2"
        filter_candidates.append(("format", fallback_filter))

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
            if encoder_name == "hevc_rkmpp":
                cmd.extend(["-profile:v", "main", "-tag:v", "hvc1"])
                bv, maxrate, bufsize = self._hevc_rate_control(target_w)
                cmd.extend(["-b:v", bv, "-maxrate", maxrate, "-bufsize", bufsize])
            else:
                cmd.extend(["-b:v", str(profile.video_bitrate)])
            cmd.extend(
                [
                    "-g",
                    "240",
                    "-movflags",
                    "+faststart",
                    "-c:a",
                    "aac",
                    "-b:a",
                    "128k",
                    str(dest_path),
                ]
            )
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
                    should_cancel=lambda: self._is_cancelled(record),
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

    def _transcode_jetson(
        self,
        record,
        source_path: Path,
        dest_path: Path,
        profile: QualityProfile,
        codec: CodecPreference,
    ) -> None:
        scaled_w, scaled_h = self._compute_scaled_dimensions(record, profile)
        target_w, target_h = profile.width, profile.height
        xpos = max(0, (target_w - scaled_w) // 2)
        ypos = max(0, (target_h - scaled_h) // 2)
        encoder = "nvv4l2h265enc" if codec == CodecPreference.h265 else "nvv4l2h264enc"
        parser = "h265parse" if codec == CodecPreference.h265 else "h264parse"
        video_only = dest_path.with_name(f"{dest_path.stem}.jetson-video.mp4")
        source_uri = source_path.resolve().as_uri()
        gst_cmd = [
            self.settings.gstreamer_command,
            "-q",
            "-e",
            "uridecodebin",
            f"uri={source_uri}",
            "!",
            "queue",
            "!",
            "watchdog",
            f"timeout={self.settings.media_no_progress_timeout_seconds * 1000}",
            "!",
            "nvvidconv",
            "compute-hw=0",
            "interpolation-method=2",
            "!",
            f"video/x-raw(memory:NVMM),format=NV12,width={scaled_w},height={scaled_h}",
            "!",
            "nvcompositor",
            "name=comp",
            "background=black",
            f"sink_0::xpos={xpos}",
            f"sink_0::ypos={ypos}",
            f"sink_0::width={scaled_w}",
            f"sink_0::height={scaled_h}",
            "!",
            f"video/x-raw(memory:NVMM),format=RGBA,width={target_w},height={target_h}",
            "!",
            "nvvidconv",
            "compute-hw=0",
            "!",
            f"video/x-raw(memory:NVMM),format=NV12,width={target_w},height={target_h}",
            "!",
            encoder,
            "maxperf-enable=true",
            f"bitrate={profile.video_bitrate}",
            "control-rate=1",
            "iframeinterval=240",
            "insert-sps-pps=true",
            "!",
            parser,
            "config-interval=-1",
            "!",
            "qtmux",
            "faststart=true",
            "!",
            "filesink",
            f"location={video_only}",
        ]
        environment = os.environ.copy()
        environment["GST_PLUGIN_FEATURE_RANK"] = "nvv4l2decoder:MAX"
        logger.info(
            "Running Jetson GStreamer",
            extra={"backend": "nvv4l2", "encoder": encoder, "target": f"{target_w}x{target_h}"},
        )
        try:
            self._run_gstreamer(
                gst_cmd,
                action=f"transcode-nvv4l2-{codec.value}",
                env=environment,
                should_cancel=lambda: self._is_cancelled(record),
            )
            if not video_only.exists() or video_only.stat().st_size == 0:
                raise RuntimeError("Jetson GStreamer produced no video output")

            mux_cmd = [
                self.settings.ffmpeg_command,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(video_only),
                "-i",
                str(source_path),
                "-map",
                "0:v:0",
                "-map",
                "1:a:0?",
                "-c:v",
                "copy",
                "-c:a",
                "aac",
                "-b:a",
                "128k",
                "-shortest",
                "-movflags",
                "+faststart",
            ]
            if codec == CodecPreference.h265:
                mux_cmd.extend(["-tag:v", "hvc1"])
            mux_cmd.append(str(dest_path))
            duration = record.media_duration_seconds or 0.0
            self._run_ffmpeg(
                mux_cmd,
                action="mux-jetson-audio",
                progress_handler=lambda seconds: self._update_progress(record, seconds, duration),
                should_cancel=lambda: self._is_cancelled(record),
            )
        finally:
            try:
                video_only.unlink()
            except FileNotFoundError:
                pass

    def _run_gstreamer(
        self,
        cmd: list[str],
        *,
        action: str,
        env: dict[str, str],
        should_cancel: Callable[[], bool] | None = None,
    ) -> None:
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
        )
        output_lines: list[str] = []
        started_at = time.monotonic()
        try:
            while process.poll() is None:
                if should_cancel and should_cancel():
                    self._terminate_process(process, action)
                    raise TranscodeCancelled(f"GStreamer {action} cancelled")
                if time.monotonic() - started_at >= self.settings.media_command_timeout_seconds:
                    self._terminate_process(process, action)
                    raise MediaCommandTimeout(
                        f"media_command_timeout: GStreamer {action} exceeded "
                        f"{self.settings.media_command_timeout_seconds}s"
                    )
                if not process.stdout:
                    continue
                ready, _, _ = select.select([process.stdout], [], [], 1.0)
                if not ready:
                    continue
                line = process.stdout.readline()
                if line:
                    output_lines.append(line)
            if process.stdout:
                output_lines.extend(process.stdout.readlines())
            return_code = process.wait()
        finally:
            if process.stdout:
                process.stdout.close()
        if return_code != 0:
            output_data = "".join(output_lines[-80:])
            logger.error(
                "GStreamer command failed (%s): %s",
                action,
                output_data.strip(),
                extra={"action": action, "gstreamer_output": output_data},
            )
            if "watchdog triggered" in output_data.lower():
                raise MediaCommandTimeout(
                    f"media_no_progress_timeout: GStreamer {action} produced no frames for "
                    f"{self.settings.media_no_progress_timeout_seconds}s"
                )
            raise RuntimeError(f"GStreamer {action} failed")

    def _output_matches_profile(
        self,
        output_path: Path,
        profile: QualityProfile,
    ) -> tuple[bool, tuple[int, int] | None]:
        """Check whether the rendered output respects the requested profile constraints."""

        try:
            info = probe_media(output_path)
        except ProbeError as exc:
            logger.warning(
                "Unable to probe hardware output",
                extra={"path": str(output_path), "error": str(exc)},
            )
            return False, None

        video = info.video
        if not video or video.width is None or video.height is None:
            logger.warning(
                "Hardware output probe missing video dimensions",
                extra={"path": str(output_path)},
            )
            return False, None

        within_bounds = video.width <= profile.width and video.height <= profile.height
        return within_bounds, (video.width, video.height)

    def _run_ffmpeg(
        self,
        cmd: list[str],
        action: str,
        env: dict[str, str] | None = None,
        progress_handler: Callable[[float], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> None:
        logger.info("Running ffmpeg", extra={"action": action, "command": " ".join(cmd)})
        wrapped_cmd = [cmd[0], "-progress", "pipe:1", "-nostats"] + cmd[1:]
        process = subprocess.Popen(
            wrapped_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
        )
        output_lines: list[str] = []
        started_at = time.monotonic()
        last_progress_at = started_at
        try:
            while process.poll() is None:
                if should_cancel and should_cancel():
                    self._terminate_process(process, action)
                    raise TranscodeCancelled(f"ffmpeg {action} cancelled")
                now = time.monotonic()
                if now - started_at >= self.settings.media_command_timeout_seconds:
                    self._terminate_process(process, action)
                    raise MediaCommandTimeout(
                        f"media_command_timeout: ffmpeg {action} exceeded "
                        f"{self.settings.media_command_timeout_seconds}s"
                    )
                if now - last_progress_at >= self.settings.media_no_progress_timeout_seconds:
                    self._terminate_process(process, action)
                    raise MediaCommandTimeout(
                        f"media_no_progress_timeout: ffmpeg {action} made no progress for "
                        f"{self.settings.media_no_progress_timeout_seconds}s"
                    )

                if not process.stdout:
                    continue

                ready, _, _ = select.select([process.stdout], [], [], 1.0)
                if not ready:
                    continue

                line = process.stdout.readline()
                if not line:
                    continue
                if self._handle_ffmpeg_output_line(line, output_lines, progress_handler):
                    last_progress_at = time.monotonic()

            if process.stdout:
                for line in process.stdout:
                    self._handle_ffmpeg_output_line(line, output_lines, progress_handler)
            return_code = process.wait()
        finally:
            if process.stdout:
                process.stdout.close()

        if return_code != 0:
            output_data = "".join(output_lines[-80:])
            logger.error(
                "ffmpeg command failed (%s): %s",
                action,
                output_data.strip(),
                extra={"action": action, "ffmpeg_output": output_data},
            )
            raise RuntimeError(f"ffmpeg {action} failed")

    def _handle_ffmpeg_output_line(
        self,
        line: str,
        output_lines: list[str],
        progress_handler: Callable[[float], None] | None,
    ) -> bool:
        if line.startswith("out_time_ms="):
            try:
                microseconds = int(line.strip().split("=", 1)[1])
                seconds = microseconds / 1_000_000
                if progress_handler:
                    progress_handler(seconds)
                return True
            except ValueError:
                return False
        elif not line.startswith(("frame=", "fps=", "stream_", "progress=", "bitrate=", "total_size=", "out_time_")):
            output_lines.append(line)
        return False

    def _terminate_process(self, process: subprocess.Popen[str], action: str) -> None:
        logger.info("Terminating media command", extra={"action": action})
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            logger.warning("Killing unresponsive media command", extra={"action": action})
            process.kill()
            process.wait(timeout=10)

    def _is_cancelled(self, record) -> bool:
        return bool(
            getattr(record, "cancel_requested", False) or getattr(record, "status", None) == JobStatus.cancelled
        )

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
                timeout=self.settings.ffprobe_timeout_seconds,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
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

    def _select_hardware_codecs(
        self,
        info: MediaInfo,
        target_codec: CodecPreference,
    ) -> tuple[str | None, str, str]:
        if self.settings.worker_backend == "rkmpp":
            use_hw, decoder, encoder = self._select_rkmpp_codecs(info, target_codec)
            return ("rkmpp" if use_hw else None), decoder, encoder
        if self.settings.worker_backend == "nvv4l2":
            use_hw, decoder, encoder = self._select_jetson_codecs(info, target_codec)
            return ("nvv4l2" if use_hw else None), decoder, encoder
        return None, "", ""

    def _select_jetson_codecs(
        self,
        info: MediaInfo,
        target_codec: CodecPreference,
    ) -> tuple[bool, str, str]:
        encoder = "nvv4l2h265enc" if target_codec == CodecPreference.h265 else "nvv4l2h264enc"
        if not gstreamer_feature_available(self.settings, encoder):
            return False, "", ""
        decoder = ""
        source_codec = (info.video.codec_name or "").lower() if info.video else ""
        normalized_codec = normalize_video_codec(source_codec)
        if normalized_codec in JETSON_DECODERS and gstreamer_feature_available(self.settings, "nvv4l2decoder"):
            decoder = "nvv4l2decoder"
        return bool(decoder), decoder, encoder

    def _validate_backend_input_codec(self, info: MediaInfo) -> None:
        if self.settings.worker_backend != "nvv4l2" or self.settings.allow_cpu_fallback or not info.video:
            return
        codec = normalize_video_codec(info.video.codec_name) or "unknown"
        if codec not in JETSON_DECODERS:
            raise UnsupportedInputCodec(codec, "Jetson NVDEC")

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

    def _compute_scaled_dimensions(self, record, profile: QualityProfile) -> tuple[int, int]:
        target_w = profile.width
        target_h = profile.height
        src_w = record.source_width or target_w
        src_h = record.source_height or target_h
        if not target_w or not target_h or not src_w or not src_h:
            return target_w, target_h
        ratio = min(target_w / src_w, target_h / src_h)
        ratio = min(ratio, 1.0)
        scaled_w = max(16, int(round(src_w * ratio / 2) * 2))
        scaled_h = max(16, int(round(src_h * ratio / 2) * 2))
        scaled_w = min(scaled_w, target_w)
        scaled_h = min(scaled_h, target_h)
        return scaled_w, scaled_h

    def _map_codec_name(self, codec: str | None) -> CodecPreference | None:
        if not codec:
            return None
        value = codec.lower()
        if value in {"h264", "avc1", "avc"}:
            return CodecPreference.h264
        if value in {"h265", "hevc", "hvc1"}:
            return CodecPreference.h265
        return None
