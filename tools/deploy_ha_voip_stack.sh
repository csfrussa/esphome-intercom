#!/usr/bin/env bash
set -euo pipefail

HA_HOST="${HA_HOST:-hass}"
HA_COMPONENT_DIR="${HA_COMPONENT_DIR:-/var/lib/hass/custom_components/voip_stack}"
HA_SERVICE="${HA_SERVICE:-home-assistant.service}"
LOCAL_COMPONENT_DIR="custom_components/voip_stack"
REMOTE_STAGE="/tmp/voip-stack-deploy-${USER:-codex}-$$"

if [[ ! -f "$LOCAL_COMPONENT_DIR/manifest.json" ]]; then
  echo "Run this command from the esphome-intercom repository root." >&2
  exit 2
fi

cleanup() {
  ssh "$HA_HOST" "rm -rf -- '$REMOTE_STAGE'" >/dev/null 2>&1 || true
}
trap cleanup EXIT

# The HA configuration tree belongs to the hass user and is intentionally not
# writable through SCP. Always stage as the SSH user, then install atomically
# through the host's passwordless sudo path.
tar \
  --exclude='__pycache__' \
  --exclude='*.pyc' \
  -C "$LOCAL_COMPONENT_DIR" -cf - . |
  ssh "$HA_HOST" "mkdir -p '$REMOTE_STAGE' && tar -C '$REMOTE_STAGE' -xf -"

ssh "$HA_HOST" "
  set -eu
  sudo -n mkdir -p '$HA_COMPONENT_DIR'
  sudo -n rsync -a --delete --exclude='__pycache__/' '$REMOTE_STAGE/' '$HA_COMPONENT_DIR/'
  sudo -n chown -R hass:hass '$HA_COMPONENT_DIR'
  sudo -n systemctl restart '$HA_SERVICE'
  for attempt in \$(seq 1 90); do
    state=\$(systemctl is-active '$HA_SERVICE' 2>/dev/null || true)
    if [ \"\$state\" = active ]; then
      echo 'Home Assistant active; VoIP Stack deployment complete.'
      exit 0
    fi
    if [ \"\$state\" = failed ]; then
      sudo -n journalctl -u '$HA_SERVICE' -n 80 --no-pager >&2
      exit 1
    fi
    sleep 1
  done
  echo 'Timed out waiting for Home Assistant to become active.' >&2
  exit 1
"
