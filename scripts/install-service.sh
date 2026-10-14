#!/usr/bin/env bash
set -euo pipefail
_world_script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$_world_script_dir/install_service.py" "$@"
