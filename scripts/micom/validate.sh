#!/usr/bin/env bash
set -euo pipefail
root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
# Portable canonicalization (macOS realpath lacks -m); python3 is required anyway.
canon() { python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$1"; }
image=$(canon "${1:?Usage: validate.sh image frames.jsonl out-dir}")
frames=$(canon "${2:?Usage: validate.sh image frames.jsonl out-dir}")
out=$(canon "${3:?Usage: validate.sh image frames.jsonl out-dir}")
# Validator creates a new child directory and refuses to overwrite existing output.
mkdir -p "$out"
docker run --rm --network none --read-only --cap-drop ALL --security-opt no-new-privileges \
  --pids-limit 64 --memory 1g --cpus 2 --user "$(id -u):$(id -g)" --entrypoint python3 \
  --mount "type=bind,src=$root/scripts/micom,dst=/app,readonly" \
  --mount "type=bind,src=$image,dst=/firmware.bin,readonly" \
  --mount "type=bind,src=$frames,dst=/frames.jsonl,readonly" \
  --mount "type=bind,src=$out,dst=/out" \
  micom-emu:local /app/validate.py /firmware.bin /frames.jsonl --out /out/checks
