"""Constants for Intercom Native integration."""

import json
from pathlib import Path

DOMAIN = "intercom_native"
CONF_ASSIST_INTENTS = "assist_intents"
CONF_DEBUG_MODE = "debug_mode"
CONF_SIP_TCP_ENABLED = "sip_tcp_enabled"
CONF_SIP_UDP_ENABLED = "sip_udp_enabled"
CONF_TRUNK_ENABLED = "trunk_enabled"
CONF_TRUNK_TRANSPORT = "trunk_transport"
CONF_TRUNK_SERVER = "trunk_server"
CONF_TRUNK_PORT = "trunk_port"
CONF_TRUNK_DOMAIN = "trunk_domain"
CONF_TRUNK_USERNAME = "trunk_username"
CONF_TRUNK_AUTH_USERNAME = "trunk_auth_username"
CONF_TRUNK_PASSWORD = "trunk_password"
CONF_TRUNK_EXPIRES = "trunk_register_expires"
CONF_TRUNK_OUTBOUND_PROXY = "trunk_outbound_proxy"
CONF_TRUNK_INBOUND_DEFAULT_TARGET = "trunk_inbound_default_target"
CONF_TRUNK_DTMF_ENABLED = "trunk_dtmf_enabled"
CONF_TRUNK_DTMF_TIMEOUT_MS = "trunk_dtmf_timeout_ms"
CONF_TRUNK_DTMF_TERMINATOR = "trunk_dtmf_terminator"
CONF_TRUNK_DTMF_ROUTES = "trunk_dtmf_routes"

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
