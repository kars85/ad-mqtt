# Project awareness — ad-mqtt

MQTT bridge for AlarmDecoder, coupled to the **alarmdecoder library** and to **ser2sock**.
Before changing coupled behavior, read the contract on the other side.

**Read `../alarmdecoder/docs/ecosystem.md` first** — the local sibling copy of the
[alarmdecoder ecosystem map](https://github.com/kars85/alarmdecoder/blob/main/docs/ecosystem.md)
for all three repos.

## Couplings

- **alarmdecoder library** (pinned in `requirements.txt` to
  `git+https://github.com/kars85/alarmdecoder@1.14.0`, a Git-tag-only release on a
  private repo — installs need git auth) — `ad_mqtt/run.py` uses
  public `decoder.wire_events()` because `ad_mqtt/Client.py` duck-types the device API and
  owns its lifecycle through the asyncio supervisor coroutine in `ad_mqtt/run.py`.
  `ad_mqtt/Bridge.py` consumes exactly
  23 events plus `Message`/`RFMessage` attributes. `tests/test_alarmdecoder_contract.py`
  guards those subscriptions, callback signatures, representative panel/RF flow, and
  panel-specific command bytes.
- **ser2sock** — `ad_mqtt/Client.py` uses plaintext TCP with newline framing and no TLS. If the webapp
  turns on ser2sock SSL, this bridge cannot connect.
- **Panel is DSC hardware.** Bridge command paths fail closed until the AD2 `CONFIG`
  response and a later matching KPM confirm panel mode/state, then choose DSC or Ademco
  Vista sequences — see Phase 2 item 0 in the modernization plan. Non-authoritative KPMs
  still flow to MQTT; they cannot authorize commands. Physical writes default off through
  `ADMQTT_COMMANDS_ENABLED=false`; Bridge then subscribes to no command topics and Client
  permits only AD2 `C`/`V` queries. Enabled writes require Bridge's exact one-shot transport
  authorization, local-socket acceptance of the full segment, and every expected per-key AD2
  send report. DSC actions and chime also require a later terminal KPM; Vista actions release
  after their send reports. Because `!Sending` has no correlation token, enabled operation
  requires this bridge to be the exclusive AD2/ser2sock writer. The runner consumes
  `ADMQTT_COMMANDS_UNLOCK_FILE` once so restarts fail closed.
- **Home Assistant** — MQTT discovery payloads in `ad_mqtt/Discovery.py` are the external
  contract; Bridge topic attributes are retained state HA depends on.
- **alarmdecoder-webapp** — no direct coupling; shares ser2sock and the physical AD2 device.

## The rule

Before renaming topics, discovery `unique_id`s, or payload shapes: these are retained in the
broker and mirrored in HA — plan a migration (clear retained, republish). Before bumping the
alarmdecoder dependency: diff its event/attribute surface against `Bridge._connect()` and
run the consumer contract tests. Do not replace explicit `wire_events()` with
`AlarmDecoder.open()` while `Client` remains a supervisor-owned duck type. MQTT rides
`ad_mqtt/Mqtt.py` (paho-mqtt 2.x, paho's network thread + `call_soon_threadsafe`
marshaling onto the asyncio loop); `clean_session=True` is a G9 safety requirement —
never make the command session persistent.

The focused library contract tests use standard-library `unittest` and in-memory fakes;
they need no MQTT broker or ser2sock instance. Run them against the pinned, installed
distribution; the sibling-tree form is a development convenience:

```bash
PYTHONPATH=../alarmdecoder python3 -m unittest discover -s tests -v
```

## Repo state notes

- The insteon-mqtt fork is gone: transport is asyncio + paho-mqtt 2.x
  (`ad_mqtt/Signal.py`, `ad_mqtt/Mqtt.py`, supervisor in `ad_mqtt/run.py`). Zones load
  from YAML (`ADMQTT_DEVICES_FILE`, `zones.yaml.example`); the alarm code loads from a
  restricted file (`ADMQTT_ALARM_CODE_FILE`, G15). Version is 2.0.0 (unreleased).
- The safe AlarmDecoder compatibility slice is implemented and the dependency is pinned
  to released tag `1.14.0`; contract tests pass against the installed wheel. The suite
  now also needs `paho-mqtt>=2` and `pyyaml` installed.
- DSC/Vista command-byte and duplicate-delivery protection are implemented. The exact DSC
  model/zone width, panel-programming settings, and physically attended canary remain hard
  gates before setting `ADMQTT_COMMANDS_ENABLED=true`. No published image contains this
  safety revision, and a branch commit alone is not a deployable artifact. Independently
  review, hardware-canary, version, and tag it before deployment.
- `../alarmdecoder/docs/ecosystem.md` still needs its release, panel-family, dependency-pin,
  and event-count entries synchronized before it can serve as rollout authority.
- Modernization plan: [`docs/modernization-plan.md`](docs/modernization-plan.md).
