# Testing And Debug

This project has enough call paths that manual spot checks are not enough.
Use this page as the standard regression checklist before release-level
changes.

## Automated Local Tests

Run the HA integration suite:

```bash
cd <checkout>/esphome-intercom
./.venv/bin/python -m pytest tests -q
```

Important groups:

- `tests/test_voip_backend_route_contract.py`: static contracts for SIP route
  branches and service registration.
- `tests/test_voip_phase1.py`: resolver, registrar, RTP relay and protocol
  behavior.
- `tests/test_group_call_matrix.py`: PBX-style ring/conference group matrix.
- `tests/test_conference.py`: conference mixer and lifecycle primitives.

## Real SIP Matrix

The development environment can run local SIP endpoints against the real HA
instance. The useful matrix is:

- create multiple SIP endpoint accounts;
- register them to HA over SIP/TCP;
- call by name;
- call by extension;
- call HA by name;
- call HA by extension;
- change HA extension and verify immediate phonebook/dial-plan update;
- verify registered endpoint calls do not fall into `route_requested`.

Expected route evidence:

- endpoint-to-endpoint:
  `SIP TX INVITE <callee>@<registered-contact-host>:<port>`;
- HA target:
  `HA softphone state=ringing`;
- no `SIP route requested` for registered endpoint calls to normal roster
  targets.

## Service Matrix

Exercise all public services with temporary data:

- schema list contains every expected service;
- `set_dnd`;
- `set_ha_softphone_settings`;
- `create_account`, `disable_account`, `enable_account`,
  `rotate_account_password`, `list_accounts`, `export_accounts`,
  `remove_account`;
- `add_contact`, `remove_contact`, `set_contacts`, `clear_contacts`,
  `export_phonebook`, `push_phonebook`;
- `call` and `forward` to a registered endpoint;
- `answer`, `decline`, `hangup` against pending SIP calls;
- `route` against a forced route request;
- `purge_devices` with a high `min_unavailable_hours` as a no-op.

Always restore:

- HA softphone extension and group settings;
- DND off;
- manual phonebook contacts;
- temporary SIP accounts removed;
- no pending HA softphone call.

## Home Assistant Logs

Useful filters:

```bash
journalctl -u home-assistant.service --since "10 minutes ago" --no-pager |
  grep -E "SIP RX INVITE|SIP TX INVITE|SIP TX 180|SIP route requested|SIP bridge registered|HA softphone state|registered user"
```

Run that command on a host using the documented systemd service layout. On
Home Assistant OS, containers or other installations, use **Settings → System
→ Logs** or the installation's supported log command instead of assuming a
host name or log-file path.

Look for:

- `SIP bridge registered` after outbound bridge setup;
- `SIP TX 180 Ringing` immediately after `answer_ha` pending calls;
- `SIP TX INVITE <target>@<contact>` for registered endpoint routes;
- `SIP route requested` only for explicit automation fallback scenarios.

## Phonebook Inspection

```bash
export HA_URL="https://home-assistant.example"
read -rsp "Home Assistant long-lived access token: " HA_TOKEN; echo
curl -fsS -H "Authorization: Bearer $HA_TOKEN" \
  "$HA_URL/api/states/sensor.voip_phonebook" |
  jq -r '.attributes.roster_json' | jq '.contacts[] | {id,name,extension,sip_uri,metadata}'
unset HA_TOKEN
```

Do not commit tokens, private host names, IP addresses or secret-file paths to
the repository.

Use this after every group/extension/account change. The phonebook is the
source of truth for dialing.

## Runtime Snapshots

ESP devices expose useful SIP snapshots as sensors:

- `sensor.<device>_voip_state`
- `sensor.<device>_voip_endpoint`
- `sensor.<device>_voip_sip_snapshot`
- `sensor.<device>_voip_last_reason`
- `text.<device>_voip_ring_groups`
- `text.<device>_voip_conference_groups`
- `switch.<device>_voip_conference_ring`

For HA-side runtime, inspect call events, softphone state events and
`sensor.voip_phonebook`.

## Audio Debug

When RTP relay debug is enabled, HA writes WAV captures under:

```text
~/.cache/voip_stack_debug/
```

The filenames include source/destination call IDs and side labels. Use these
when a call connects but audio direction, volume or format negotiation is
unclear.

Captures are opt-in through `debug_mode`. The directory is created with mode
`0700`, names are sanitized, and pruning keeps at most the newest 24 files and
64 MiB in total. WAV/JSON data can still contain private conversation audio and
call metadata: disable debug mode after the test and remove retained artifacts
according to the deployment's privacy policy.

## Serial And Device Debug

For ESP debug:

- serial logs show component setup, SIP state, audio stack state and reset
  causes;
- JTAG snapshots are useful when a device is responsive enough to expose
  runtime state but audio or FSM state is inconsistent;
- keep volumes low for automated tests, but do not set them to zero when
  validating real audio paths.

When testing real devices, cover:

- HA to ESP;
- ESP to HA;
- ESP to ESP;
- registered SIP endpoint to ESP;
- registered SIP endpoint to HA;
- HA/card to registered SIP endpoint;
- unknown, unregistered SIP endpoint to HA;
- unknown, unregistered SIP endpoint to each ESP;
- in-dialog hold/re-INVITE receives `488` while the established call and later
  BYE remain functional;
- ring group caller cancel before answer;
- ring group first-answer-wins;
- conference join/leave;
- group membership changes reflected in the phonebook.
