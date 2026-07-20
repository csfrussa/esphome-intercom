/**
 * Pure data normalization shared by the VoIP Stack card and its editor.
 *
 * Keep this module independent from Home Assistant and browser globals so the
 * same roster entry always produces the same dialing target in every UI path.
 */

export function formatListFromMetadata(value) {
  if (Array.isArray(value)) return value.filter(Boolean).map((item) => String(item));
  if (typeof value === "string") {
    return value.split(";").map((item) => item.trim()).filter(Boolean);
  }
  return [];
}

export function targetFromRosterEntry(entry) {
  const metadata = entry?.metadata || {};
  const id = entry?.id || entry?.name;
  const signaling = metadata.sip_transport || metadata.signaling_transport || "";
  return {
    endpoint_id: metadata.endpoint_id || "",
    device_id: metadata.device_id || id,
    name: entry?.name || id,
    route_id: id,
    host: entry?.address || "",
    sip_transport: signaling,
    sip_uri: entry?.sip_uri || "",
    extension: entry?.extension || "",
    number: entry?.number || "",
    ha_bridge: !!entry?.ha_bridge,
    endpoint_kind: String(metadata.endpoint_kind || "").trim().toLowerCase(),
    capabilities: formatListFromMetadata(metadata.capabilities)
      .map((value) => value.toLowerCase()),
    audio_mode: metadata.audio_mode || "full_duplex",
    tx_formats: formatListFromMetadata(metadata.tx_formats),
    rx_formats: formatListFromMetadata(metadata.rx_formats),
    sip_port: entry?.port || metadata.port || metadata.sip_port,
    rtp_port: metadata.rtp_port,
    max_payload_bytes: metadata.max_payload_bytes,
    roster: true,
  };
}

export function targetSupportsVideo(target) {
  const capabilities = Array.isArray(target?.capabilities)
    ? target.capabilities.map((value) => String(value).trim().toLowerCase()).filter(Boolean)
    : [];
  if (capabilities.length) return capabilities.includes("video");

  // ESPHome endpoints are explicitly audio-only. Unknown/manual SIP targets
  // remain eligible because their remote capabilities are learned through SDP.
  return String(target?.endpoint_kind || "").trim().toLowerCase() !== "esphome";
}

export function normaliseTransport(value) {
  const transport = String(value || "").trim().toLowerCase();
  return ["tcp", "udp", "sip_tcp", "sip_udp"].includes(transport)
    ? transport.replace(/^sip_/, "").toUpperCase()
    : "";
}

export function normaliseAudioMode(value) {
  const mode = String(value || "").trim().toLowerCase();
  return ["full_duplex", "mic_only", "speaker_only", "control_only"].includes(mode)
    ? mode
    : "full_duplex";
}

export function audioModeLabel(mode) {
  switch (normaliseAudioMode(mode)) {
    case "mic_only": return "MIC";
    case "speaker_only": return "SPK";
    case "control_only": return "CTRL";
    default: return "FULL";
  }
}
