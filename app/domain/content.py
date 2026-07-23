from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> tuple[str, int]:
    """Return the SHA-256 digest and byte length of a file."""

    digest = hashlib.sha256()
    size_bytes = 0
    with path.open("rb") as source:
        while chunk := source.read(chunk_size):
            digest.update(chunk)
            size_bytes += len(chunk)
    return digest.hexdigest(), size_bytes


def content_addressed_key(sha256: str) -> str:
    """Return the immutable S3 key for an exact-byte blob identity."""

    normalized = sha256.lower()
    if len(normalized) != 64 or any(character not in "0123456789abcdef" for character in normalized):
        raise ValueError("sha256 must contain exactly 64 hexadecimal characters")
    return f"blobs/sha256/{normalized[:2]}/{normalized}"


def canonical_json(value: Mapping[str, Any]) -> str:
    """Serialize cache-key input deterministically."""

    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def pipeline_run_key(
    *,
    source_sha256: str,
    pipeline_name: str,
    pipeline_version: str,
    options: Mapping[str, Any],
    processor_versions: Mapping[str, str] | None = None,
    schema_version: str = "1",
) -> str:
    """Return a deterministic cache key for one versioned pipeline execution."""

    payload = {
        "options": options,
        "pipeline_name": pipeline_name,
        "pipeline_version": pipeline_version,
        "processor_versions": dict(processor_versions or {}),
        "schema_version": schema_version,
        "source_sha256": source_sha256.lower(),
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
