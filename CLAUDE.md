# Project awareness — ad-mqtt

MQTT bridge for AlarmDecoder, coupled to the **alarmdecoder library** and to **ser2sock**.
Before changing coupled behavior, read the contract on the other side.

**Read [`alarmdecoder/docs/ecosystem.md`](../alarmdecoder/docs/ecosystem.md) first** — hub map
of all three repos.

## Couplings

- **alarmdecoder library** (pinned in `requirements.txt` to
  `git+https://github.com/kars85/alarmdecoder@1.14.0`, a Git-tag-only release on a
  private repo — installs need git auth) — `ad_mqtt/run.py` uses
  public `decoder.wire_events()` because `ad_mqtt/Client.py` duck-types the device API and
  owns its lifecycle through the legacy poll manager. `ad_mqtt/Bridge.py` consumes exactly
  21 events plus `Message`/`RFMessage` attributes. `tests/test_alarmdecoder_contract.py`
  guards those subscriptions, callback signatures, and representative panel/RF flow.
- **ser2sock** — `ad_mqtt/Client.py:61` plaintext TCP, newline framing, no TLS. If the webapp
  turns on ser2sock SSL, this bridge cannot connect.
- **Home Assistant** — MQTT discovery payloads in `ad_mqtt/Discovery.py` are the external
  contract; topics in `ad_mqtt/Bridge.py:25-39` are retained state HA depends on.
- **alarmdecoder-webapp** — no direct coupling; shares ser2sock and the physical AD2 device.

## The rule

Before renaming topics, discovery `unique_id`s, or payload shapes: these are retained in the
broker and mirrored in HA — plan a migration (clear retained, republish). Before bumping the
alarmdecoder dependency: diff its event/attribute surface against `Bridge._connect()` and
run the consumer contract tests. Do not replace explicit `wire_events()` with
`AlarmDecoder.open()` while `Client` remains a poll-manager-owned duck type.

The focused library contract tests use standard-library `unittest` and in-memory fakes;
they need no MQTT broker or ser2sock instance. Run them against the pinned, installed
distribution; the sibling-tree form is a development convenience:

```bash
PYTHONPATH=../alarmdecoder python3 -m unittest discover -s tests -v
```

## Repo state notes

- Depends on a personal fork of insteon-mqtt (`f1d094/...paho-mqtt-1.6.1`, unpinned HEAD)
  purely for its select loop + paho wrapper — top modernization target.
- The safe AlarmDecoder compatibility slice is implemented and the dependency is pinned
  to released tag `1.14.0`; contract tests pass against the installed wheel.
- Modernization plan: [`docs/modernization-plan.md`](docs/modernization-plan.md).
