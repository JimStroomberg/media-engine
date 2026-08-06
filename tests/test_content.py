from __future__ import annotations

import hashlib

import pytest

from app.domain.content import canonical_json, content_addressed_key, pipeline_run_key, sha256_file
from app.domain.pipelines import get_pipeline


def test_sha256_file_returns_digest_and_size(tmp_path) -> None:
    path = tmp_path / "sample.bin"
    payload = b"media-engine\x00sample"
    path.write_bytes(payload)

    digest, size_bytes = sha256_file(path)

    assert digest == hashlib.sha256(payload).hexdigest()
    assert size_bytes == len(payload)


def test_content_addressed_key_uses_digest_prefix() -> None:
    digest = "ab" + "1" * 62

    assert content_addressed_key(digest) == f"blobs/sha256/ab/{digest}"


@pytest.mark.parametrize("value", ["", "z" * 64, "a" * 63, "a" * 65])
def test_content_addressed_key_rejects_invalid_digest(value: str) -> None:
    with pytest.raises(ValueError):
        content_addressed_key(value)


def test_canonical_json_is_key_order_independent() -> None:
    assert canonical_json({"b": 2, "a": {"d": 4, "c": 3}}) == canonical_json({"a": {"c": 3, "d": 4}, "b": 2})


def test_pipeline_run_key_is_stable_for_equivalent_options() -> None:
    common = {
        "source_sha256": "a" * 64,
        "pipeline_name": "ai_prepare",
        "pipeline_version": "1",
        "processor_versions": {"ffmpeg": "7.1", "schema": "1"},
    }

    first = pipeline_run_key(options={"ocr": True, "sampling": {"mode": "scene"}}, **common)
    second = pipeline_run_key(options={"sampling": {"mode": "scene"}, "ocr": True}, **common)

    assert first == second


def test_pipeline_version_changes_run_key() -> None:
    base = {
        "source_sha256": "a" * 64,
        "pipeline_name": "transcode",
        "options": {"codec": "h265"},
    }

    assert pipeline_run_key(pipeline_version="1", **base) != pipeline_run_key(pipeline_version="2", **base)


def test_registered_pipeline_versions_can_be_used_in_run_key() -> None:
    pipeline = get_pipeline("transcode")

    key = pipeline_run_key(
        source_sha256="a" * 64,
        pipeline_name=pipeline.name,
        pipeline_version=pipeline.version,
        options={"codec": "h265"},
        processor_versions=pipeline.processor_versions,
        schema_version=pipeline.schema_version,
    )

    assert len(key) == 64


def test_pipeline_option_defaults_have_one_run_identity() -> None:
    pipeline = get_pipeline("transcode")
    common = {
        "source_sha256": "a" * 64,
        "pipeline_name": pipeline.name,
        "pipeline_version": pipeline.version,
        "processor_versions": pipeline.processor_versions,
        "schema_version": pipeline.schema_version,
    }

    omitted = pipeline_run_key(options=pipeline.normalize_options({}), **common)
    explicit = pipeline_run_key(
        options=pipeline.normalize_options({"quality": "auto", "codec": "auto", "quality_profile": "balanced"}),
        **common,
    )

    assert omitted == explicit
