#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

SCENARIO="all"
REPEAT=1
SEED=""
MODE="all"
SOCKET="${SIM_SOCKET:-test_runs/simulator/intercom-sim.sock}"
CONTRACT=0

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
    --contract)
      CONTRACT=1
      shift
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
python3 tools/simulator/scenario_runner.py "$SCENARIO" --validate-only

CONTRACT_PID=""
if [[ "$CONTRACT" == "1" ]]; then
  rm -f "$SOCKET"
  python3 tools/simulator/contract_simulator.py --socket "$SOCKET" &
  CONTRACT_PID="$!"
  for _ in $(seq 1 50); do
    [[ -S "$SOCKET" ]] && break
    sleep 0.1
  done
  trap '[[ -n "$CONTRACT_PID" ]] && python3 tools/simulator/simctl.py --socket "$SOCKET" shutdown >/dev/null 2>&1 || true; [[ -n "$CONTRACT_PID" ]] && wait "$CONTRACT_PID" >/dev/null 2>&1 || true' EXIT
fi

if [[ ! -S "$SOCKET" ]]; then
  echo "virtual device is not running: $SOCKET" >&2
  echo "Phase 00V scaffold is installed, but host backend executable is not implemented yet." >&2
  echo "Use --contract to run the deterministic contract simulator while the host backend is incomplete." >&2
  exit 2
fi

args=(tools/simulator/scenario_runner.py "$SCENARIO" --socket "$SOCKET" --repeat "$REPEAT")
if [[ -n "$SEED" ]]; then
  args+=(--seed "$SEED")
fi

echo "mode=$MODE scenario=$SCENARIO repeat=$REPEAT seed=${SEED:-none}"
python3 "${args[@]}"
