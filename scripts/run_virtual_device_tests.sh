#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

SCENARIO="all"
REPEAT=1
SEED=""
MODE="all"
SOCKET="${SIM_SOCKET:-test_runs/simulator/intercom-sim.sock}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --changed)
      MODE="changed"
      shift
      ;;
    --all)
      MODE="all"
      shift
      ;;
    --scenario)
      SCENARIO="${2:?missing scenario name}"
      shift 2
      ;;
    --repeat)
      REPEAT="${2:?missing repeat count}"
      shift 2
      ;;
    --seed)
      SEED="${2:?missing seed}"
      shift 2
      ;;
    --socket)
      SOCKET="${2:?missing socket path}"
      shift 2
      ;;
    *)
      echo "unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

mkdir -p test_runs/simulator

python3 tools/simulator/generate_virtual_profiles.py --limit 3 >/tmp/intercom_virtual_profiles.txt
python3 tools/simulator/simctl.py --socket "$SOCKET" doctor

if [[ ! -S "$SOCKET" ]]; then
  echo "virtual device is not running: $SOCKET" >&2
  echo "Phase 00V scaffold is installed, but host backend executable is not implemented yet." >&2
  exit 2
fi

args=(tools/simulator/scenario_runner.py "$SCENARIO" --socket "$SOCKET" --repeat "$REPEAT")
if [[ -n "$SEED" ]]; then
  args+=(--seed "$SEED")
fi

echo "mode=$MODE scenario=$SCENARIO repeat=$REPEAT seed=${SEED:-none}"
python3 "${args[@]}"
