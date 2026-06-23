#!/usr/bin/env bash
set -Eeuo pipefail

usage() {
  cat <<'EOF'
Usage:
  scripts/build_deploy_ap.sh --allow-flash [options]

Build the AP OpenWrt profile, read the exported firmware artifact from the
build report, then flash that exact artifact to the DUT.

Options:
  --allow-flash          Required for real DUT deploy. Passed to owrt-monitor flash.
  --config PATH          Config file. Default: configs/example.yaml
  --profile NAME         Profile to build and flash. Default: ap-be5000
  --owrt-monitor PATH    owrt-monitor executable. Default: $OWRT_MONITOR or owrt-monitor
  --flash-dry-run        Run the flash phase as a dry-run instead of flashing.
  -h, --help             Show this help.

Examples:
  scripts/build_deploy_ap.sh --allow-flash
  scripts/build_deploy_ap.sh --config configs/example.yaml --profile ap-be5000 --allow-flash
  scripts/build_deploy_ap.sh --flash-dry-run
EOF
}

config="configs/example.yaml"
profile="ap-be5000"
owrt_monitor="${OWRT_MONITOR:-owrt-monitor}"
allow_flash=0
flash_dry_run=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --allow-flash)
      allow_flash=1
      shift
      ;;
    --config)
      config="${2:?--config requires a path}"
      shift 2
      ;;
    --config=*)
      config="${1#--config=}"
      shift
      ;;
    --profile)
      profile="${2:?--profile requires a name}"
      shift 2
      ;;
    --profile=*)
      profile="${1#--profile=}"
      shift
      ;;
    --owrt-monitor)
      owrt_monitor="${2:?--owrt-monitor requires a path}"
      shift 2
      ;;
    --owrt-monitor=*)
      owrt_monitor="${1#--owrt-monitor=}"
      shift
      ;;
    --flash-dry-run)
      flash_dry_run=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ "$flash_dry_run" -eq 0 && "$allow_flash" -eq 0 ]]; then
  echo "Refusing to deploy without --allow-flash. Use --flash-dry-run to preview only." >&2
  exit 2
fi

if [[ ! -f "$config" ]]; then
  echo "Config not found: $config" >&2
  exit 2
fi

build_output="$(mktemp -t owrt-ap-build.XXXXXX)"
cleanup() {
  rm -f "$build_output"
}
trap cleanup EXIT

echo "==> Building OpenWrt profile '$profile'"
"$owrt_monitor" build --config "$config" --profile "$profile" | tee "$build_output"

run_dir="$(
  python3 - "$build_output" <<'PY'
from __future__ import annotations

import re
import sys

text = open(sys.argv[1], encoding="utf-8", errors="replace").read()
text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
match = re.search(r"^Run directory:\s*(.+?)\s*$", text, re.MULTILINE)
print(match.group(1).strip() if match else "")
PY
)"

if [[ -z "$run_dir" ]]; then
  echo "Could not find build run directory in owrt-monitor output." >&2
  exit 1
fi

report_json="$run_dir/report.json"
if [[ ! -f "$report_json" ]]; then
  echo "Build report not found: $report_json" >&2
  exit 1
fi

artifact="$(
  python3 - "$report_json" <<'PY'
from __future__ import annotations

import json
import sys

with open(sys.argv[1], encoding="utf-8") as fh:
    report = json.load(fh)
artifact = report.get("artifact") or {}
host_path = artifact.get("host_path") or ""
print(host_path)
PY
)"

if [[ -z "$artifact" ]]; then
  echo "Build report did not include artifact.host_path: $report_json" >&2
  exit 1
fi

if [[ ! -f "$artifact" ]]; then
  echo "Artifact file does not exist: $artifact" >&2
  exit 1
fi

echo "==> Deploying artifact to DUT"
echo "Artifact: $artifact"

flash_args=(flash --config "$config" --profile "$profile" --artifact "$artifact")
if [[ "$flash_dry_run" -eq 1 ]]; then
  flash_args+=(--dry-run)
else
  flash_args+=(--allow-flash)
fi

"$owrt_monitor" "${flash_args[@]}"
