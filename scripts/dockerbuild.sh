#!/bin/bash
set -euo pipefail

TAG="${1:-}"
if [[ -z "${TAG}" ]]; then
  echo "Usage: $0 <tag>" >&2
  echo "Optional env vars:" >&2
  echo "  IMAGE_REPO   (default jimstro/media-engine)" >&2
  echo "  BASE_PLATFORMS (default linux/amd64,linux/arm64)" >&2
  echo "  RK_PLATFORMS   (default linux/arm64)" >&2
  echo "  PUSH=true|false (default true)" >&2
  exit 1
fi

IMAGE_REPO="${IMAGE_REPO:-jimstro/media-engine}"
BASE_PLATFORMS="${BASE_PLATFORMS:-linux/amd64,linux/arm64}"
RK_PLATFORMS="${RK_PLATFORMS:-linux/arm64}"
PUSH="${PUSH:-true}"

BUILD_CMD=(docker buildx build)
if [[ "${PUSH}" == "true" ]]; then
  BUILD_CMD+=(--push)
else
  BUILD_CMD+=(--load)
fi

# Generic multi-arch image
"${BUILD_CMD[@]}" \
  --platform "${BASE_PLATFORMS}" \
  --build-arg RK_VARIANT=false \
  -t "${IMAGE_REPO}:${TAG}" \
  -t "${IMAGE_REPO}:latest" \
  -f Dockerfile \
  .

# Rockchip RK1 optimized image
"${BUILD_CMD[@]}" \
  --platform "${RK_PLATFORMS}" \
  --build-arg RK_VARIANT=true \
  -t "${IMAGE_REPO}:rk1-${TAG}" \
  -t "${IMAGE_REPO}:rk1-latest" \
  -f Dockerfile \
  .
