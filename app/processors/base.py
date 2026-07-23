from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


@dataclass(frozen=True)
class StageInputFile:
    artifact_type: str
    path: Path
    media_type: str | None
    format: str
    schema_version: str | None = None
    producer_stage: str | None = None


@dataclass(frozen=True)
class ProducedArtifact:
    artifact_type: str
    path: Path
    media_type: str
    format: str
    schema_version: str = "1"


class StageProcessor(Protocol):
    async def process(
        self,
        processor: str,
        *,
        inputs: dict[str, StageInputFile],
        options: dict[str, Any],
        output_dir: Path,
    ) -> list[ProducedArtifact]: ...


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
        encoding="utf-8",
    )


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))
