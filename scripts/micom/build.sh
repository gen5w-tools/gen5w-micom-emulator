#!/usr/bin/env bash
set -euo pipefail
root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
docker build -t micom-emu:local -f "$root/docker/Dockerfile.micom" "$root/scripts/micom"
