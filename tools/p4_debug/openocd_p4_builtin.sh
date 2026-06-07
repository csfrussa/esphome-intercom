#!/usr/bin/env bash
set -euo pipefail

OPENOCD="${OPENOCD:-openocd}"
args=()
if [[ -n "${OPENOCD_SCRIPTS:-}" ]]; then
  args+=(-s "$OPENOCD_SCRIPTS")
fi

exec "$OPENOCD" \
  "${args[@]}" \
  -f board/esp32p4-builtin.cfg \
  -c "adapter speed 4000"
