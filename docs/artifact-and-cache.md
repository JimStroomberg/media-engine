# Artifact, duplicate detection, and retention contract

## Content identity

Every durable object is identified by the SHA-256 digest of its exact bytes. S3 ETags are not content identities because multipart uploads and provider implementations can produce different ETag semantics.

The API calculates SHA-256 while receiving an upload. Exact duplicates reference the existing available blob rather than storing another copy.

Source metadata such as provider, source ID, URL, title, uploader, chapters, and subtitles is retained as a lookup hint and provenance. It does not replace content-hash verification because the same source can be downloaded at different qualities or in different containers.

## Pipeline run identity

A pipeline run key is the SHA-256 digest of a canonical document containing:

- source blob SHA-256;
- pipeline name and version;
- canonical pipeline options;
- relevant processor, provider, prompt, and model versions;
- output schema version.

If a matching run is completed and every required artifact is still available, a new request attaches to that run and reports `cache_hit=true`. If the run is active, the new request attaches to the in-flight run. A database uniqueness constraint enforces this single-flight behavior.

Pipeline options are validated and normalized by a server-owned schema before hashing. Omitted defaults and explicitly supplied defaults therefore have the same run identity, while unsupported options are rejected rather than silently ignored.

Changing a pipeline, provider, prompt, model, option, or output schema creates a different run key and permits intentional reprocessing.

## S3 key layout

Durable blobs use content-addressed keys:

```text
blobs/sha256/<first-two-hex-characters>/<full-sha256>
```

Database records map human-facing assets and artifacts to these immutable blob keys. Temporary upload keys and incomplete multipart uploads are never considered available artifacts.

Clients retrieve artifacts through short-lived signed S3 URLs. The public signing endpoint is separately configurable from the container-internal S3 endpoint so signatures remain valid from outside the Compose network.

## Retention

Every asset and artifact has an explicit `expires_at`. The default policy is a fixed interval from successful ingest or processing completion. A new live reference may extend a shared blob's retention, but reads do not silently extend expiry.

Deletion states are `available`, `expiring`, `deleting`, and `expired`. The scheduler:

1. selects expired records without active stage leases or pins;
2. marks them `deleting`;
3. deletes the S3 object;
4. verifies or accepts an idempotent not-found response;
5. marks the database record `expired`.

Ingest and deletion take the same PostgreSQL row lock for an existing content hash. The scheduler holds that lock through the S3 deletion and state commit, so expiry cannot remove an object being concurrently reused or restored.

Small historical records may outlive media objects. An old successful job is not a cache hit when any required artifact has expired or disappeared.

S3 lifecycle rules are a safety net for abandoned multipart uploads and unexpectedly orphaned objects. Application state remains authoritative. Disposable-media buckets should disable versioning or expire non-current versions so delete markers do not retain disk usage indefinitely.
