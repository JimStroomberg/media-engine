from __future__ import annotations

import asyncio
import csv
import json
import math
import re
import zipfile
from io import StringIO
from pathlib import Path, PurePosixPath
from typing import Any

from .base import ProducedArtifact, StageInputFile, read_json, write_json


class StageProcessingError(RuntimeError):
    pass


class LocalMediaProcessor:
    def __init__(self, *, ffmpeg_command: str, ffprobe_command: str, tesseract_command: str, timeout: int) -> None:
        self.ffmpeg_command = ffmpeg_command
        self.ffprobe_command = ffprobe_command
        self.tesseract_command = tesseract_command
        self.timeout = timeout

    async def process(
        self,
        processor: str,
        *,
        inputs: dict[str, StageInputFile],
        options: dict[str, Any],
        output_dir: Path,
    ) -> list[ProducedArtifact]:
        output_dir.mkdir(parents=True, exist_ok=True)
        handlers = {
            "probe": self._probe,
            "audio_extract": self._audio_extract,
            "subtitle_extract": self._subtitle_extract,
            "scene_detect": self._scene_detect,
            "keyframe_extract": self._keyframe_extract,
            "coarse_keyframe_extract": self._coarse_keyframe_extract,
            "adaptive_keyframe_extract": self._adaptive_keyframe_extract,
            "ocr": self._ocr,
            "coarse_ocr": self._coarse_ocr,
        }
        try:
            handler = handlers[processor]
        except KeyError as exc:
            raise StageProcessingError(f"Unsupported local stage processor: {processor}") from exc
        return await handler(inputs=inputs, options=options, output_dir=output_dir)

    async def _probe(
        self,
        *,
        inputs: dict[str, StageInputFile],
        options: dict[str, Any],
        output_dir: Path,
    ) -> list[ProducedArtifact]:
        del options
        source = self._required_input(inputs, "source")
        stdout, _ = await self._run(
            self.ffprobe_command,
            "-v",
            "error",
            "-show_format",
            "-show_streams",
            "-show_chapters",
            "-of",
            "json",
            str(source.path),
        )
        raw = json.loads(stdout)
        format_data = dict(raw.get("format") or {})
        format_data.pop("filename", None)
        streams = list(raw.get("streams") or [])
        duration = self._duration_seconds(format_data, streams)
        payload = {
            "schema_version": "1",
            "duration_seconds": duration,
            "container": format_data,
            "streams": streams,
            "chapters": list(raw.get("chapters") or []),
            "has_audio": any(stream.get("codec_type") == "audio" for stream in streams),
            "has_video": any(stream.get("codec_type") == "video" for stream in streams),
            "has_subtitles": any(stream.get("codec_type") == "subtitle" for stream in streams),
        }
        path = output_dir / "media_metadata.json"
        write_json(path, payload)
        return [ProducedArtifact("media_metadata", path, "application/json", "json")]

    async def _audio_extract(
        self,
        *,
        inputs: dict[str, StageInputFile],
        options: dict[str, Any],
        output_dir: Path,
    ) -> list[ProducedArtifact]:
        source = self._required_input(inputs, "source")
        metadata = read_json(self._required_input(inputs, "media_metadata").path)
        audio_streams = [stream for stream in metadata.get("streams", []) if stream.get("codec_type") == "audio"]
        audio_metadata_path = output_dir / "audio_metadata.json"
        result = [ProducedArtifact("audio_metadata", audio_metadata_path, "application/json", "json")]
        if not audio_streams:
            write_json(
                audio_metadata_path,
                {
                    "schema_version": "1",
                    "has_audio": False,
                    "duration_seconds": metadata.get("duration_seconds"),
                    "streams": [],
                },
            )
            return result

        bitrate = int(options["audio_bitrate_kbps"])
        audio_path = output_dir / "audio.mp3"
        await self._run(
            self.ffmpeg_command,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source.path),
            "-map",
            "0:a:0",
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "libmp3lame",
            "-b:a",
            f"{bitrate}k",
            str(audio_path),
        )
        write_json(
            audio_metadata_path,
            {
                "schema_version": "1",
                "has_audio": True,
                "duration_seconds": metadata.get("duration_seconds"),
                "codec": "mp3",
                "bitrate_kbps": bitrate,
                "sample_rate_hz": 16000,
                "channels": 1,
                "streams": audio_streams,
            },
        )
        result.append(ProducedArtifact("audio", audio_path, "audio/mpeg", "mp3"))
        return result

    async def _subtitle_extract(
        self,
        *,
        inputs: dict[str, StageInputFile],
        options: dict[str, Any],
        output_dir: Path,
    ) -> list[ProducedArtifact]:
        del options
        source = self._required_input(inputs, "source")
        metadata = read_json(self._required_input(inputs, "media_metadata").path)
        subtitle_streams = [stream for stream in metadata.get("streams", []) if stream.get("codec_type") == "subtitle"]
        subtitles: list[dict[str, Any]] = []
        for stream in subtitle_streams:
            index = int(stream["index"])
            destination = output_dir / f"subtitle-{index}.vtt"
            try:
                await self._run(
                    self.ffmpeg_command,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-i",
                    str(source.path),
                    "-map",
                    f"0:{index}",
                    "-f",
                    "webvtt",
                    str(destination),
                )
            except StageProcessingError as exc:
                subtitles.append(
                    {
                        "stream_index": index,
                        "codec": stream.get("codec_name"),
                        "language": (stream.get("tags") or {}).get("language"),
                        "status": "unsupported",
                        "error": str(exc),
                    }
                )
                continue
            subtitles.append(
                {
                    "stream_index": index,
                    "codec": stream.get("codec_name"),
                    "language": (stream.get("tags") or {}).get("language"),
                    "title": (stream.get("tags") or {}).get("title"),
                    "status": "available",
                    "format": "webvtt",
                    "text": destination.read_text(encoding="utf-8", errors="replace"),
                }
            )
        path = output_dir / "subtitles.json"
        write_json(path, {"schema_version": "1", "tracks": subtitles})
        return [ProducedArtifact("subtitles", path, "application/json", "json")]

    async def _scene_detect(
        self,
        *,
        inputs: dict[str, StageInputFile],
        options: dict[str, Any],
        output_dir: Path,
    ) -> list[ProducedArtifact]:
        source = self._required_input(inputs, "source")
        metadata = read_json(self._required_input(inputs, "media_metadata").path)
        duration = float(metadata.get("duration_seconds") or 0.0)
        threshold = float(options["scene_threshold"])
        timestamps: list[float] = []
        if metadata.get("has_video"):
            _, stderr = await self._run(
                self.ffmpeg_command,
                "-hide_banner",
                "-nostats",
                "-i",
                str(source.path),
                "-vf",
                f"select=gt(scene\\,{threshold}),showinfo",
                "-an",
                "-f",
                "null",
                "-",
            )
            timestamps = [float(value) for value in re.findall(r"pts_time:([0-9]+(?:\.[0-9]+)?)", stderr)]
        scenes = [{"start_seconds": timestamp} for timestamp in self._unique_timestamps(timestamps, duration)]
        path = output_dir / "scenes.json"
        write_json(
            path,
            {
                "schema_version": "1",
                "duration_seconds": duration,
                "threshold": threshold,
                "has_video": bool(metadata.get("has_video")),
                "scenes": scenes,
            },
        )
        return [ProducedArtifact("scenes", path, "application/json", "json")]

    async def _keyframe_extract(
        self,
        *,
        inputs: dict[str, StageInputFile],
        options: dict[str, Any],
        output_dir: Path,
    ) -> list[ProducedArtifact]:
        source = self._required_input(inputs, "source")
        scene_data = read_json(self._required_input(inputs, "scenes").path)
        duration = float(scene_data.get("duration_seconds") or 0.0)
        interval = float(options["keyframe_interval_seconds"])
        max_keyframes = int(options["max_keyframes"])
        timestamps = [float(scene["start_seconds"]) for scene in scene_data.get("scenes", [])]
        if scene_data.get("has_video", True):
            timestamps.extend(self._interval_timestamps(duration, interval))
        timestamps = self._limit_evenly(self._unique_timestamps(timestamps, duration), max_keyframes)

        return await self._write_keyframes(
            source=source,
            duration=duration,
            selections=[{"timestamp_seconds": timestamp} for timestamp in timestamps],
            max_width=int(options["keyframe_max_width"]),
            output_dir=output_dir,
            archive_name="keyframes.zip",
            index_name="keyframe_index.json",
            archive_artifact_type="keyframes",
            index_artifact_type="keyframe_index",
        )

    async def _coarse_keyframe_extract(
        self,
        *,
        inputs: dict[str, StageInputFile],
        options: dict[str, Any],
        output_dir: Path,
    ) -> list[ProducedArtifact]:
        source = self._required_input(inputs, "source")
        scene_data = read_json(self._required_input(inputs, "scenes").path)
        duration = float(scene_data.get("duration_seconds") or 0.0)
        timestamps = [float(scene["start_seconds"]) for scene in scene_data.get("scenes", [])]
        if scene_data.get("has_video", True):
            timestamps.extend(self._interval_timestamps(duration, float(options["keyframe_interval_seconds"])))
        timestamps = self._limit_evenly(
            self._unique_timestamps(timestamps, duration),
            int(options["coarse_max_keyframes"]),
        )
        return await self._write_keyframes(
            source=source,
            duration=duration,
            selections=[{"timestamp_seconds": timestamp} for timestamp in timestamps],
            max_width=int(options["keyframe_max_width"]),
            output_dir=output_dir,
            archive_name="coarse_keyframes.zip",
            index_name="coarse_keyframe_index.json",
            archive_artifact_type="coarse_keyframes",
            index_artifact_type="coarse_keyframe_index",
        )

    async def _adaptive_keyframe_extract(
        self,
        *,
        inputs: dict[str, StageInputFile],
        options: dict[str, Any],
        output_dir: Path,
    ) -> list[ProducedArtifact]:
        source = self._required_input(inputs, "source")
        scene_data = read_json(self._required_input(inputs, "scenes").path)
        content_plan = read_json(self._required_input(inputs, "content_plan").path)
        duration = float(scene_data.get("duration_seconds") or 0.0)
        selections = self._adaptive_frame_selections(
            scene_data,
            content_plan,
            max_keyframes=int(options["max_keyframes"]),
            targeted_keyframes=int(options["targeted_keyframes"]),
            fallback_interval=float(options["keyframe_interval_seconds"]),
        )
        return await self._write_keyframes(
            source=source,
            duration=duration,
            selections=selections,
            max_width=int(options["keyframe_max_width"]),
            output_dir=output_dir,
            archive_name="keyframes.zip",
            index_name="keyframe_index.json",
            archive_artifact_type="keyframes",
            index_artifact_type="keyframe_index",
        )

    async def _write_keyframes(
        self,
        *,
        source: StageInputFile,
        duration: float,
        selections: list[dict[str, Any]],
        max_width: int,
        output_dir: Path,
        archive_name: str,
        index_name: str,
        archive_artifact_type: str,
        index_artifact_type: str,
    ) -> list[ProducedArtifact]:

        frame_dir = output_dir / "frames"
        frame_dir.mkdir(parents=True, exist_ok=True)
        index_entries: list[dict[str, Any]] = []
        frame_paths: list[Path] = []
        for frame_number, selection in enumerate(selections):
            timestamp = float(selection["timestamp_seconds"])
            filename = f"frame-{frame_number:04d}.jpg"
            frame_path = frame_dir / filename
            try:
                await self._run(
                    self.ffmpeg_command,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-ss",
                    f"{timestamp:.3f}",
                    "-i",
                    str(source.path),
                    "-frames:v",
                    "1",
                    "-vf",
                    f"scale=min({max_width}\\,iw):-2",
                    "-q:v",
                    "3",
                    str(frame_path),
                )
            except StageProcessingError:
                continue
            if not frame_path.exists() or frame_path.stat().st_size == 0:
                continue
            frame_paths.append(frame_path)
            index_entry = {
                "frame_id": frame_path.stem,
                "filename": filename,
                "timestamp_seconds": timestamp,
            }
            if selection.get("selection_reasons"):
                index_entry["selection_reasons"] = selection["selection_reasons"]
            index_entries.append(index_entry)

        zip_path = output_dir / archive_name
        with zipfile.ZipFile(zip_path, "w") as archive:
            for frame_path in frame_paths:
                info = zipfile.ZipInfo(frame_path.name, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, frame_path.read_bytes())

        index_path = output_dir / index_name
        write_json(
            index_path,
            {
                "schema_version": "1",
                "duration_seconds": duration,
                "frames": index_entries,
            },
        )
        return [
            ProducedArtifact(archive_artifact_type, zip_path, "application/zip", "zip"),
            ProducedArtifact(index_artifact_type, index_path, "application/json", "json"),
        ]

    async def _ocr(
        self,
        *,
        inputs: dict[str, StageInputFile],
        options: dict[str, Any],
        output_dir: Path,
    ) -> list[ProducedArtifact]:
        return await self._write_ocr(
            inputs=inputs,
            options=options,
            output_dir=output_dir,
            archive_artifact_type="keyframes",
            index_artifact_type="keyframe_index",
            output_artifact_type="ocr",
            output_name="ocr.json",
        )

    async def _coarse_ocr(
        self,
        *,
        inputs: dict[str, StageInputFile],
        options: dict[str, Any],
        output_dir: Path,
    ) -> list[ProducedArtifact]:
        return await self._write_ocr(
            inputs=inputs,
            options=options,
            output_dir=output_dir,
            archive_artifact_type="coarse_keyframes",
            index_artifact_type="coarse_keyframe_index",
            output_artifact_type="coarse_ocr",
            output_name="coarse_ocr.json",
        )

    async def _write_ocr(
        self,
        *,
        inputs: dict[str, StageInputFile],
        options: dict[str, Any],
        output_dir: Path,
        archive_artifact_type: str,
        index_artifact_type: str,
        output_artifact_type: str,
        output_name: str,
    ) -> list[ProducedArtifact]:
        index_data = read_json(self._required_input(inputs, index_artifact_type).path)
        frames_by_name = {frame["filename"]: frame for frame in index_data.get("frames", [])}
        extracted_dir = output_dir / "ocr-frames"
        extracted_dir.mkdir(parents=True, exist_ok=True)
        results: list[dict[str, Any]] = []
        language = str(options["ocr_language"])

        with zipfile.ZipFile(self._required_input(inputs, archive_artifact_type).path) as archive:
            for member in archive.infolist():
                if PurePosixPath(member.filename).name != member.filename or member.filename not in frames_by_name:
                    raise StageProcessingError("Keyframe archive contains an unexpected path")
                image_path = extracted_dir / member.filename
                image_path.write_bytes(archive.read(member))
                stdout, _ = await self._run(
                    self.tesseract_command,
                    str(image_path),
                    "stdout",
                    "-l",
                    language,
                    "tsv",
                )
                words = self._parse_tesseract_tsv(stdout)
                results.append(
                    {
                        "frame_id": frames_by_name[member.filename]["frame_id"],
                        "timestamp_seconds": frames_by_name[member.filename]["timestamp_seconds"],
                        "text": " ".join(word["text"] for word in words),
                        "words": words,
                    }
                )

        path = output_dir / output_name
        write_json(
            path,
            {
                "schema_version": "1",
                "engine": "tesseract",
                "language": language,
                "frames": results,
            },
        )
        return [ProducedArtifact(output_artifact_type, path, "application/json", "json")]

    async def _run(self, *command: str) -> tuple[str, str]:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(process.communicate(), timeout=self.timeout)
        except TimeoutError as exc:
            process.kill()
            await process.communicate()
            raise StageProcessingError(f"Media command exceeded {self.timeout} seconds") from exc
        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")
        if process.returncode != 0:
            message = stderr.strip()[-2000:] or f"command exited with status {process.returncode}"
            raise StageProcessingError(message)
        return stdout, stderr

    @staticmethod
    def _required_input(inputs: dict[str, StageInputFile], artifact_type: str) -> StageInputFile:
        try:
            return inputs[artifact_type]
        except KeyError as exc:
            raise StageProcessingError(f"Stage input is missing required artifact: {artifact_type}") from exc

    @staticmethod
    def _duration_seconds(format_data: dict[str, Any], streams: list[dict[str, Any]]) -> float | None:
        candidates = [format_data.get("duration"), *(stream.get("duration") for stream in streams)]
        durations: list[float] = []
        for candidate in candidates:
            try:
                durations.append(float(candidate))
            except (TypeError, ValueError):
                continue
        return max(durations) if durations else None

    @staticmethod
    def _unique_timestamps(values: list[float], duration: float) -> list[float]:
        bounded = {round(value, 3) for value in values if math.isfinite(value) and 0 <= value < max(duration, 0.001)}
        if duration > 0:
            bounded.add(0.0)
        return sorted(bounded)

    @staticmethod
    def _interval_timestamps(duration: float, interval: float) -> list[float]:
        if duration <= 0:
            return []
        return [index * interval for index in range(math.ceil(duration / interval))]

    @classmethod
    def _adaptive_frame_selections(
        cls,
        scene_data: dict[str, Any],
        content_plan: dict[str, Any],
        *,
        max_keyframes: int,
        targeted_keyframes: int,
        fallback_interval: float,
    ) -> list[dict[str, Any]]:
        duration = float(scene_data.get("duration_seconds") or 0.0)
        if duration <= 0 or not scene_data.get("has_video", True):
            return []

        priority_order = {"high": 0, "medium": 1, "low": 2}
        target_candidates: list[dict[str, Any]] = []
        for target in content_plan.get("target_moments") or []:
            try:
                timestamp = round(float(target["timestamp_seconds"]), 3)
            except (KeyError, TypeError, ValueError):
                continue
            if not math.isfinite(timestamp) or not 0 <= timestamp < duration:
                continue
            priority = str(target.get("priority") or "medium")
            target_candidates.append(
                {
                    "timestamp_seconds": timestamp,
                    "priority": priority,
                    "reason": str(target.get("reason") or "planner target")[:240],
                }
            )
        target_candidates.sort(
            key=lambda item: (
                priority_order.get(item["priority"], priority_order["medium"]),
                item["timestamp_seconds"],
            )
        )

        selections_by_timestamp: dict[float, dict[str, Any]] = {}
        target_limit = min(max_keyframes, targeted_keyframes)
        for target in target_candidates:
            timestamp = target["timestamp_seconds"]
            if timestamp in selections_by_timestamp:
                continue
            selections_by_timestamp[timestamp] = {
                "timestamp_seconds": timestamp,
                "selection_reasons": [f"target:{target['priority']}:{target['reason']}"],
            }
            if len(selections_by_timestamp) >= target_limit:
                break

        try:
            planned_interval = float(content_plan.get("sampling_interval_seconds"))
        except (TypeError, ValueError):
            planned_interval = fallback_interval
        if not math.isfinite(planned_interval) or planned_interval < 0.5:
            planned_interval = fallback_interval
        planned_interval = min(planned_interval, 600.0)

        scene_timestamps = cls._unique_timestamps(
            [float(scene["start_seconds"]) for scene in scene_data.get("scenes", [])],
            duration,
        )
        interval_timestamps = cls._unique_timestamps(
            cls._interval_timestamps(duration, planned_interval),
            duration,
        )
        baseline_candidates = cls._unique_timestamps(scene_timestamps + interval_timestamps, duration)
        baseline_candidates = [
            timestamp
            for timestamp in baseline_candidates
            if all(abs(timestamp - selected) >= 0.5 for selected in selections_by_timestamp)
        ]
        available_slots = max(0, max_keyframes - len(selections_by_timestamp))
        for timestamp in cls._limit_evenly(baseline_candidates, available_slots):
            reasons: list[str] = []
            if timestamp in scene_timestamps:
                reasons.append("scene-change")
            if timestamp in interval_timestamps:
                reasons.append("interval")
            selections_by_timestamp[timestamp] = {
                "timestamp_seconds": timestamp,
                "selection_reasons": reasons or ["baseline"],
            }

        return [selections_by_timestamp[timestamp] for timestamp in sorted(selections_by_timestamp)]

    @staticmethod
    def _limit_evenly(values: list[float], limit: int) -> list[float]:
        if limit <= 0:
            return []
        if len(values) <= limit:
            return values
        if limit == 1:
            return [values[0]]
        indexes = {round(index * (len(values) - 1) / (limit - 1)) for index in range(limit)}
        return [values[index] for index in sorted(indexes)]

    @staticmethod
    def _parse_tesseract_tsv(value: str) -> list[dict[str, Any]]:
        words: list[dict[str, Any]] = []
        for row in csv.DictReader(StringIO(value), delimiter="\t"):
            text = (row.get("text") or "").strip()
            if not text:
                continue
            try:
                confidence = float(row.get("conf") or -1)
            except ValueError:
                confidence = -1
            words.append(
                {
                    "text": text,
                    "confidence": confidence,
                    "left": int(row.get("left") or 0),
                    "top": int(row.get("top") or 0),
                    "width": int(row.get("width") or 0),
                    "height": int(row.get("height") or 0),
                }
            )
        return words
