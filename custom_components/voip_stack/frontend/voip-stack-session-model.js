/** Pure logical-phone identity rules for the page-level softphone engine. */

export const DEFAULT_SOFTPHONE_ENDPOINT_ID = "default";

export function normaliseSoftphoneSelector(selector = {}) {
  const deviceId = String(selector?.device_id || "").trim();
  let endpointId = String(selector?.endpoint_id || "").trim();
  if (!endpointId && !deviceId) endpointId = DEFAULT_SOFTPHONE_ENDPOINT_ID;
  return { endpoint_id: endpointId, device_id: deviceId };
}

export function softphoneScopeKey(selector = {}) {
  const normalised = normaliseSoftphoneSelector(selector);
  return normalised.endpoint_id
    ? `endpoint:${normalised.endpoint_id}`
    : `device:${normalised.device_id}`;
}

export function softphoneStateMatches(
  state,
  selector = {},
  subscriptionSelector = null,
) {
  if (!state) return false;
  const wanted = normaliseSoftphoneSelector(selector);
  const source = normaliseSoftphoneSelector(subscriptionSelector || {});
  const stateEndpoint = String(state.endpoint_id || "").trim();
  const stateDevice = String(
    state.device_id || state.endpoint_device_id || "",
  ).trim();
  if (wanted.endpoint_id) {
    if (stateEndpoint) return stateEndpoint === wanted.endpoint_id;
    // Endpoint-less legacy state belongs exclusively to the historical
    // default phone and must never leak into another logical softphone.
    return wanted.endpoint_id === DEFAULT_SOFTPHONE_ENDPOINT_ID &&
      (!source.endpoint_id || source.endpoint_id === DEFAULT_SOFTPHONE_ENDPOINT_ID);
  }
  if (wanted.device_id) {
    if (stateDevice) return stateDevice === wanted.device_id;
    return source.device_id === wanted.device_id && !!stateEndpoint;
  }
  return false;
}
