"""Constants for Intercom Native integration."""

import json
from pathlib import Path

DOMAIN = "intercom_native"
CONF_ASSIST_INTENTS = "assist_intents"
CONF_SIP_TCP_ENABLED = "sip_tcp_enabled"
CONF_SIP_UDP_ENABLED = "sip_udp_enabled"

# Version from manifest.json
_MANIFEST = Path(__file__).parent / "manifest.json"
with open(_MANIFEST, encoding="utf-8") as _f:
    INTEGRATION_VERSION = json.load(_f).get("version", "0.0.0")

# Frontend URL base for serving the Lovelace card
URL_BASE = "/intercom-native"
HA_PEER_FALLBACK_NAME = "intercom-native"
HA_SOFTPHONE_DEVICE_ID = "__intercom_native_ha_softphone__"

# Project default ports for the SIP phone/bridge.
INTERCOM_PORT = 5060
INTERCOM_SIP_PORT = 5060
INTERCOM_RTP_PORT = 40000

# Length limits for SIP metadata mirrored between HA and ESP snapshots.
MAX_CALL_ID_LEN = 64
MAX_ROUTE_ID_LEN = 64
MAX_NAME_LEN = 64
MAX_REASON_LEN = 160

# Timeouts
CONNECT_TIMEOUT = 5.0
PING_INTERVAL = 5.0
