#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

SCENARIO="all"
REPEAT=1
SEED=""
MODE="all"
SOCKET="${SIM_SOCKET:-test_runs/simulator/voip-sim.sock}"
CONTRACT=0
TRACE_DIR=""
PROFILE="${SIM_PROFILE:-}"
PYTHON_BIN="${PYTHON_BIN:-$ROOT/.venv/bin/python}"
ESPHOME_BIN="${ESPHOME_BIN:-$ROOT/.venv/bin/esphome}"

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
    --trace-dir)
      TRACE_DIR="${2:?missing trace dir}"
      shift 2
      ;;
    --profile)
      PROFILE="${2:?missing ESPHome Host profile path}"
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

if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "python executable not found: $PYTHON_BIN" >&2
  exit 2
fi

if [[ -z "$PROFILE" ]]; then
  "$PYTHON_BIN" tools/simulator/generate_virtual_profiles.py --limit 3 >/tmp/voip_virtual_profiles.txt
  PROFILE="$(head -n 1 /tmp/voip_virtual_profiles.txt)"
fi

"$PYTHON_BIN" tools/simulator/simctl.py --socket "$SOCKET" doctor
"$PYTHON_BIN" tools/simulator/scenario_runner.py "$SCENARIO" --validate-only

CONTRACT_PID=""
HOST_PID=""
if [[ "$CONTRACT" == "1" ]]; then
  rm -f "$SOCKET"
  "$PYTHON_BIN" tools/simulator/contract_simulator.py --socket "$SOCKET" &
  CONTRACT_PID="$!"
  for _ in $(seq 1 50); do
    [[ -S "$SOCKET" ]] && break
    sleep 0.1
  done
  trap '[[ -n "$CONTRACT_PID" ]] && "$PYTHON_BIN" tools/simulator/simctl.py --socket "$SOCKET" shutdown >/dev/null 2>&1 || true; [[ -n "$CONTRACT_PID" ]] && wait "$CONTRACT_PID" >/dev/null 2>&1 || true' EXIT
else
  if [[ ! -x "$ESPHOME_BIN" ]]; then
    echo "esphome executable not found: $ESPHOME_BIN" >&2
    exit 2
  fi
  if [[ -z "$PROFILE" || ! -f "$PROFILE" ]]; then
    echo "ESPHome Host profile not found: $PROFILE" >&2
    exit 2
  fi
  rm -f "$SOCKET"
  "$ESPHOME_BIN" run "$PROFILE" >test_runs/simulator/esphome-host.log 2>&1 &
  HOST_PID="$!"
  for _ in $(seq 1 1200); do
    if [[ -S "$SOCKET" ]]; then
      break
    fi
    if ! kill -0 "$HOST_PID" >/dev/null 2>&1; then
      echo "ESPHome host exited before creating simulator socket" >&2
      tail -n 120 test_runs/simulator/esphome-host.log >&2 || true
      exit 2
    fi
    sleep 0.1
  done
  trap '"$PYTHON_BIN" tools/simulator/simctl.py --socket "$SOCKET" shutdown >/dev/null 2>&1 || true; [[ -n "$HOST_PID" ]] && wait "$HOST_PID" >/dev/null 2>&1 || true' EXIT
fi

if [[ ! -S "$SOCKET" ]]; then
  echo "virtual device is not running: $SOCKET" >&2
  echo "ESPHome host backend did not expose the simulator JSON-RPC socket." >&2
  tail -n 120 test_runs/simulator/esphome-host.log >&2 || true
  exit 2
fi

args=(tools/simulator/scenario_runner.py "$SCENARIO" --socket "$SOCKET" --repeat "$REPEAT")
if [[ -n "$SEED" ]]; then
  args+=(--seed "$SEED")
fi
if [[ -n "$TRACE_DIR" ]]; then
  args+=(--trace-dir "$TRACE_DIR")
fi

echo "mode=$MODE scenario=$SCENARIO repeat=$REPEAT seed=${SEED:-none}"
"$PYTHON_BIN" "${args[@]}"
