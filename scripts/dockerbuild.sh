#!/bin/bash
set -euo pipefail

TAG="${1:-}"
if [[ -z "${TAG}" ]]; then
  echo "Usage: $0 <tag>" >&2
  echo "Optional env vars:" >&2
  echo "  IMAGE_REPO      (default jimstro/media-engine)" >&2
  echo "  BASE_PLATFORMS  (default linux/amd64,linux/arm64)" >&2
  echo "  RK_PLATFORMS    (default linux/arm64)" >&2
  echo "  PUSH=true|false (default true)" >&2
  echo "  SBOM=true|false (default true)" >&2
  echo "  PROV=true|false (default true)" >&2
  exit 1
fi

IMAGE_REPO="${IMAGE_REPO:-jimstro/media-engine}"
BASE_PLATFORMS="${BASE_PLATFORMS:-linux/amd64,linux/arm64}"
RK_PLATFORMS="${RK_PLATFORMS:-linux/arm64}"
PUSH="${PUSH:-true}"
SBOM="${SBOM:-true}"
PROV="${PROV:-true}"

# Separate cache tags for generic vs rk1 builds
CACHE_REF_BASE="${IMAGE_REPO}:buildcache-base"
CACHE_REF_RK="${IMAGE_REPO}:buildcache-rk1"

BUILD_CMD=(docker buildx build --pull)
# Enable SBOM and provenance if requested
[[ "${SBOM}" == "true" ]] && BUILD_CMD+=(--sbom=true)
[[ "${PROV}" == "true" ]] && BUILD_CMD+=(--provenance=true)

if [[ "${PUSH}" == "true" ]]; then
  BUILD_CMD+=(--push)
else
  # Note: --load only works for single-arch images
  BUILD_CMD+=(--load)
fi

echo ">> Building BASE (${BASE_PLATFORMS}) against latest base if available..."
"${BUILD_CMD[@]}" \
  --platform "${BASE_PLATFORMS}" \
  --cache-from "type=registry,ref=${CACHE_REF_BASE}" \
  --cache-to   "type=registry,ref=${CACHE_REF_BASE},mode=max" \
  --build-arg RK_VARIANT=false \
  -t "${IMAGE_REPO}:${TAG}" \
  -t "${IMAGE_REPO}:latest" \
  -f Dockerfile \
  .

echo ">> Building RK1 (${RK_PLATFORMS}) against latest base if available..."
"${BUILD_CMD[@]}" \
  --platform "${RK_PLATFORMS}" \
  --cache-from "type=registry,ref=${CACHE_REF_RK}" \
  --cache-to   "type=registry,ref=${CACHE_REF_RK},mode=max" \
  --build-arg RK_VARIANT=true \
  -t "${IMAGE_REPO}:rk1-${TAG}" \
  -t "${IMAGE_REPO}:rk1-latest" \
  -f Dockerfile \
  .