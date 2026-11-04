#!/usr/bin/env bash
set -euo pipefail
root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
# Portable canonicalization (macOS realpath lacks -m); python3 is required anyway.
canon() { python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$1"; }
image=$(canon "${1:?Usage: run.sh image out-dir [runner options]}")
out=$(canon "${2:?Usage: run.sh image out-dir [runner options]}")
shift 2
mkdir -p "$out"
mounts=()
options=()
while (($#)); do
  case "$1" in
    --resume|--rx-frames|--events)
      opt=$1
      path=$(canon "${2:?Missing path after $1}")
      target="/input${#mounts[@]}"
      mounts+=(--mount "type=bind,src=$path,dst=$target,readonly")
      options+=("$opt" "$target")
      shift 2
      ;;
    --uart-socket)
      path=$(canon "${2:?Missing socket path}")
      mounts+=(--mount "type=bind,src=$path,dst=/cable.sock,readonly")
      options+=(--uart-socket /cable.sock)
      shift 2
      ;;
    *) options+=("$1"); shift ;;
  esac
done
exec docker run --rm --network none --read-only --cap-drop ALL --security-opt no-new-privileges \
  --pids-limit 64 --memory 1g --cpus 2 --user "$(id -u):$(id -g)" \
  --mount "type=bind,src=$root/scripts/micom,dst=/app,readonly" \
  --mount "type=bind,src=$image,dst=/firmware.bin,readonly" \
  --mount "type=bind,src=$out,dst=/out" "${mounts[@]}" \
  micom-emu:local /firmware.bin --out /out "${options[@]}"
