#!/usr/bin/env bash
set -euo pipefail

QUALITY="auto"
CODEC="auto"
POLL_INTERVAL=10
DOWNLOAD_OUTPUT="false"
INPUT_FILE=""
MEDIA_ENGINE=""

usage() {
  cat <<USAGE
Usage: ${0##*/} --input /path/to/video --media-engine http://host:port [options]

Options:
  --quality <auto|uhd_2160p|fhd_1080p|hd_720p|sd_480p>  Desired quality preset (default: auto)
  --codec <auto|h264|h265>                               Preferred codec (default: auto)
  --poll-interval <seconds>                              Polling interval for job status (default: 3)
  --download                                             Download completed output to current directory
  -h, --help                                             Show this help text
USAGE
}

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Error: required command '$1' not found in PATH" >&2
    exit 1
  fi
}

parse_json() {
  local path="$1"
  python3 -c '
import json
import sys
path = sys.argv[1]
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)
value = data
for part in path.split("."):
    if isinstance(value, dict):
        value = value.get(part)
    else:
        value = None
        break
if value is None:
    sys.exit(0)
if isinstance(value, (dict, list)):
    print(json.dumps(value))
else:
    print(value)
' "$path"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --input)
      INPUT_FILE=${2:-}
      shift 2
      ;;
    --media-engine)
      MEDIA_ENGINE=${2:-}
      shift 2
      ;;
    --quality)
      QUALITY=${2:-}
      shift 2
      ;;
    --codec)
      CODEC=${2:-}
      shift 2
      ;;
    --poll-interval)
      POLL_INTERVAL=${2:-}
      shift 2
      ;;
    --download)
      DOWNLOAD_OUTPUT="true"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage
      exit 1
      ;;
  esac
done

if [[ -z "$INPUT_FILE" || -z "$MEDIA_ENGINE" ]]; then
  echo "Error: --input and --media-engine are required" >&2
  usage
  exit 1
fi

if [[ ! -f "$INPUT_FILE" ]]; then
  echo "Error: input file '$INPUT_FILE' not found" >&2
  exit 1
fi

require_cmd curl
require_cmd python3

MEDIA_ENGINE=${MEDIA_ENGINE%/}

response=$(curl -sSf \
  -X POST \
  -F "file=@${INPUT_FILE}" \
  -F "quality=${QUALITY}" \
  -F "codec=${CODEC}" \
  "${MEDIA_ENGINE}/jobs")

job_id=$(parse_json "job_id" <<<"$response")
if [[ -z "$job_id" ]]; then
  echo "Failed to submit job. Response: $response" >&2
  exit 1
fi

status=$(parse_json "status" <<<"$response")
printf '[%s] Job %s submitted (quality=%s codec=%s, status=%s)\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$job_id" "$QUALITY" "$CODEC" "$status"
printf 'Polling every %ss...\n' "$POLL_INTERVAL"

declare job_detail
while true; do
  sleep "$POLL_INTERVAL"
  job_detail=$(curl -sSf "${MEDIA_ENGINE}/jobs/${job_id}") || {
    echo "Error: failed to fetch job status" >&2
    exit 1
  }
  status=$(parse_json "status" <<<"$job_detail")
  error_msg=$(parse_json "error" <<<"$job_detail")
  printf '[%s] status=%s' "$(date '+%Y-%m-%d %H:%M:%S')" "$status"
  if [[ -n "$error_msg" ]]; then
    printf ' error=%s' "$error_msg"
  fi
  printf '\n'

  case "$status" in
    completed|failed|cancelled)
      break
      ;;
  esac
done

if [[ "$status" != "completed" ]]; then
  echo "Job ended with status '$status'." >&2
  if [[ -n "$error_msg" ]]; then
    echo "Error detail: $error_msg" >&2
  fi
  exit 1
fi

echo "Job ${job_id} completed successfully."

if [[ "$DOWNLOAD_OUTPUT" == "true" ]]; then
  output_name=$(parse_json "output_filename" <<<"$job_detail")
  if [[ -z "$output_name" ]]; then
    output_name="${job_id}.mp4"
  fi
  echo "Downloading output to '${output_name}'..."
  curl -sSfL "${MEDIA_ENGINE}/jobs/${job_id}/download" -o "$output_name"
  echo "Download finished: ${output_name}"
else
  output_name=$(parse_json "output_filename" <<<"$job_detail")
  output_path=$(parse_json "output_path" <<<"$job_detail")
  download_url="${MEDIA_ENGINE}/jobs/${job_id}/download"
  echo "Download URL: ${download_url}"
  if [[ -n "$output_name" ]]; then
    echo "Reported filename: ${output_name}"
  fi
  if [[ -n "$output_path" ]]; then
    echo "Output stored on server at: ${output_path}"
  fi
fi
