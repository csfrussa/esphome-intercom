"""Constants for Intercom Native integration."""

import json
from pathlib import Path

DOMAIN = "intercom_native"

# Version from manifest.json
_MANIFEST = Path(__file__).parent / "manifest.json"
with open(_MANIFEST, encoding="utf-8") as _f:
    INTEGRATION_VERSION = json.load(_f).get("version", "0.0.0")

# Frontend URL base for serving the Lovelace card
URL_BASE = "/intercom-native"
HA_PEER_FALLBACK_NAME = "intercom-native"
HA_SOFTPHONE_DEVICE_ID = "__intercom_native_ha_softphone__"

# Project default ports. TCP and UDP can both live on 6054 because they
# sit on different protocol stacks; UDP signaling moves to 6055 so audio
# and control don't collide on the same datagram socket. Per-host
# overrides flow from the config flow and endpoint phonebook, so anything
# that touches network config must read from hass.data instead of
# hard-coding these values.
INTERCOM_PORT = 6054
INTERCOM_UDP_AUDIO_PORT = 6054
INTERCOM_UDP_CONTROL_PORT = 6055

# Message types
MSG_AUDIO = 0x01
MSG_START = 0x02
MSG_HANGUP = 0x03
MSG_PING = 0x04
MSG_PONG = 0x05
MSG_ERROR = 0x06
MSG_RING = 0x07      # ESP→HA: auto_answer OFF, waiting for local answer
MSG_ANSWER = 0x08    # ESP→HA: call answered locally, start stream
MSG_DECLINE = 0x09   # Explicit call rejection with optional UTF-8 reason

# Length limits for body string fields (matches ESP intercom_protocol.h).
MAX_CALL_ID_LEN = 64
MAX_ROUTE_ID_LEN = 64
MAX_NAME_LEN = 64
MAX_REASON_LEN = 160

# Header size: u8 type | u16 length (little-endian)
HEADER_SIZE = 3

# Timeouts
CONNECT_TIMEOUT = 5.0
PING_INTERVAL = 5.0
