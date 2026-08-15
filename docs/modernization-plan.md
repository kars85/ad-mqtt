# ad-mqtt — modernization status and plan

Status reviewed 2026-08-14 against the current source tree. **All code-side work below is
implemented**: the insteon-mqtt fork is replaced by asyncio + paho-mqtt 2.x, the Phase 2
bug fixes, handoff code gates G1/G14/G15, YAML zone config, and the packaging/CI/release
work are in the tree, with the contract suite green (65 tests) and ruff clean. What remains
is not code: the physical/manual gates in `docs/dsc-cutover-handoff.md` (broker
provisioning, shadow run, panel worksheet, exclusive-writer proof, attended canary),
independent review, and cutting the immutable release tag. Version is now 2.0.0
(unreleased). Decision (see
`alarmdecoder/docs/ecosystem.md`): ad-mqtt stays the HA path (MQTT + discovery), with a
rewritten core deployed as a container in the Proxmox-VM compose stack. It becomes the
only consumer of the alarmdecoder library after the separate webapp retirement completes.

The safe AlarmDecoder compatibility slice is implemented in this tree and the library
dependency is pinned to the released `alarmdecoder` tag `1.14.0`
(https://github.com/kars85/alarmdecoder/releases/tag/1.14.0, Git-tag-only release —
no PyPI). This is not yet the full ad-mqtt v2 rewrite. Note the pin targets the
private `kars85/alarmdecoder` repository, so every install path (developer venv,
CI, Docker build) needs git auth for that host.

## External contracts to preserve

- MQTT topics (Bridge topic attributes) and HA discovery payloads/`unique_id`s
  (`ad_mqtt/Discovery.py`) — retained in the broker and mirrored as HA entities. Renames
  require a migration step (clear retained topics, let discovery recreate entities).
- Existing `ADMQTT_*` env-var config names (`run.py`) — keep working for cutover. The new
  `ADMQTT_COMMANDS_ENABLED` opt-in defaults to false and is the command-cutover gate;
  `ADMQTT_COMMANDS_UNLOCK_FILE` is consumed once per enabled process start.

## Phase 1 — dependency rescue (the load-bearing fix)

### AlarmDecoder compatibility slice

- [x] Use the library's public, idempotent `AlarmDecoder.wire_events()` while retaining
  ad-mqtt's poll-manager-owned device lifecycle.
- [x] Add `tests/test_alarmdecoder_contract.py`, covering the exact 23 Bridge event
  subscriptions and callback signatures, a raw keypad `Message` flow, and sequential
  `RFMessage` flow through the real decoder into an in-memory MQTT fake. The suite now
  also covers DSC/Vista command bytes and command safety gates.
- [x] Keep these tests independent of live MQTT/ser2sock and the legacy insteon-mqtt
  transport; they use standard-library `unittest` and add no dependency.
- [x] Pin `alarmdecoder` to the immutable modernization release once it is published.
  Do not invent or pin an unpublished version. (Pinned in `requirements.txt` to
  `alarmdecoder @ git+https://github.com/kars85/alarmdecoder@1.14.0`; the contract
  suite passes against an installed artifact from that exact tag, not only the sibling
  source path.)

Run the compatibility slice against the pinned, installed distribution
(`pip install -r requirements.txt` in a venv, then `python3 -m unittest discover -s
tests -v`). The sibling-tree form remains a development convenience only:

```bash
PYTHONPATH=../alarmdecoder python3 -m unittest discover -s tests -v
```

### Dependency rewrite — done

`insteon-mqtt` is gone. The transport is now asyncio + paho-mqtt 2.x: `ad_mqtt/Signal.py`
(minimal callback list), `ad_mqtt/Mqtt.py` (paho 2.x wrapper; paho's network thread owns
MQTT and every Bridge-facing callback is marshaled onto the asyncio loop), and a
supervisor coroutine in `ad_mqtt/run.py` that owns the Client lifecycle (readiness
dispatch via `add_reader`/`add_writer`, reconnect after close). `Client` keeps its raw
non-blocking socket and byte-level write-authorization state machine unchanged —
asyncio streams cannot express partial-send accounting/cancel. **`clean_session=True` is
a safety requirement (handoff G9): commands published while the bridge is offline are
dropped by the broker, never queued and replayed**; a wrapper test is the tripwire.
`pyyaml` is now a real dependency (zone config); `Jinja2` and the broken egg line are
gone. The public wiring migration stands; do not replace it with `AlarmDecoder.open()`.

## Phase 2 — bug fixes (from audit, keep behavior otherwise)

0. **DSC panel command sequences — implemented, hardware canary pending.** Command
   handlers now fail closed until `on_config_received` confirms the AD2 panel mode and a
   later KPM identifies the same mode and current state. Reconnect and mode changes start
   a new command-state epoch without suppressing ordinary KPM telemetry. Vista retains
   `code+2`/`code+3`/`code+7`/`code+1`; DSC uses special keys S5/S4 for away/stay,
   `*9+code` for the existing night/instant meaning, and the code alone for disarm.
   Because those inputs can toggle or change meaning in another state, every DSC arm
   requires a confirmed-disarmed panel, disarm requires a confirmed armed/triggered panel,
   and one DSC action remains interlocked until Client observes every byte accepted by its local
   socket, the AD2 reports the expected successful per-key `!Sending...done` responses, and a
   later terminal KPM arrives. Chime uses `*4` with the same causal desired-state/toggle
   protection. Vista actions are serialized through the full expected AD2 response count but do
   not wait for a terminal KPM. A wholly unsent frame is canceled if another KPM arrives; a
   partial, failed, missing-response, or otherwise ambiguous write survives a ser2sock reconnect
   and requires operator recovery. Programming-mode KPMs remain telemetry-only for both panel
   families and clear unsent chime/bypass intent rather than executing it later.

   DSC bypass builds `*1<two-digit known-faulted zones>#` on a confirmed-disarmed panel,
   rejects invalid/no-known-fault/already-bypassed states, never prefixes disarm, and is
   consumed after one arm attempt. Configured zones still unknown in the current epoch are
   not selected. The HA discovery command is no longer retained, and replayed retained ON
   messages from the old discovery contract are rejected. Retained panel actions and retained
   chime commands are also rejected, and a duplicate bypass `OFF` remains able to clear intent.
   Bypass intent is session-local and is also cleared on MQTT or ser2sock reconnect.

   Physical commands default off through `ADMQTT_COMMANDS_ENABLED=false`. In this mode Bridge
   subscribes to no command topics and Client permits only the AD2 `C`/`V` queries. Enabled mode
   accepts only exact one-shot writes authorized by Bridge, so AlarmDecoder's automatic `*`
   fault-expansion write cannot bypass the guards. Client reasserts write interest after the
   legacy poll manager registers it, preventing the startup `C`/`V` queries from remaining in a
   lost pre-registration buffer. The runner also requires and consumes an explicit
   `ADMQTT_COMMANDS_UNLOCK_FILE`; an unattended process restart therefore fails closed instead
   of losing an in-memory interlock.

   The modernization plan is not an enablement recipe. The executable procedure and complete
   preconditions are only in `docs/dsc-cutover-handoff.md`. In particular, do not begin even an
   exploratory physical canary until its G1 payload-free logging, G9 clean-session/offline-replay,
   G13 cross-repository instructions, broker/session purge, monitoring test mode, and exclusive-
   writer gates pass. A release-qualifying canary also requires the G15 restricted alarm-code
   interface. Disable immediately on any mismatch.

   The contract suite covers both command families, pre-CONFIG EXP/KPM ordering, mode
   disagreement, reconnects, programming mode, retained commands, exact transport
   authorization/cancellation, per-key AD2 acknowledgement ordering, Client's lost-write
   recovery hook at the legacy poll-manager boundary, the one-shot startup unlock, and duplicate
   DSC action/chime deliveries.
   This proves library interaction and emitted bytes, not deployed-panel semantics. Before
   production, record the exact DSC model and its bypass zone width/range, confirm
   `MODE=D`, enable the required Quick Arm/function keys, confirm whether an access code is
   required for bypass (the implemented sequence requires it to be disabled), verify the
   intended `*9` behavior, and capture the deployed AD2 firmware's `!Sending` response count for
   ordinary and S4/S5 keys. Then canary chime, bypass, stay/disarm, night/disarm, and away/disarm
   one path at a time with an operator present. Until those checks pass, leave
   `ADMQTT_COMMANDS_ENABLED=false`. The guarded branch still needs independent review,
   hardware evidence, a release identity, and an immutable tag before deployment; no
   deployable revision exists yet.
   The executable release, shadow, canary, rollback, and production checklist is in
   [`docs/dsc-cutover-handoff.md`](dsc-cutover-handoff.md).

1. **Fixed.** TLS settings now reach paho: `ad_mqtt/Mqtt.py` takes explicit kwargs, so the
   nested-`Data` `cfg.mqtt.__dict__` bug is gone by construction; a wrapper test asserts
   `tls_set` is applied when `ADMQTT_MQTT_CA_CERT` is set.
2. **Fixed.** `env_bool()` in `run.py` parses `"false"`/`"0"` correctly for
   `ADMQTT_RESTORE_ON_STARTUP` and `ADMQTT_LOG_SCREEN`.
3. **Fixed in the compatibility slice:** import and use `RotatingFileHandler` directly
   instead of relying on an unimported `logging.handlers` submodule.
4. **Fixed.** `alarm/panel/faulted` is published on alarm (payload matches the discovery
   `json_attributes_template`) and cleared on restore.
5. **Fixed.** `_update_panel_status` no longer routes through the unknown-zone skip; panel
   state (including `triggered`) publishes even for unconfigured zones.
6. **Fixed (code half of G10).** `on_alarm_restored` reports the armed state remembered
   from the latest KPM armed bits instead of forcing `"disarmed"`. Refine against captured
   panel frames during the shadow run.
7. **Fixed, including G15.** The alarm code comes from a restricted file
   (`ADMQTT_ALARM_CODE_FILE`: regular, non-symlink, owner-only permissions, non-empty;
   `Config.read_alarm_code`). With commands enabled, an environment code is refused unless
   `ADMQTT_ALARM_CODE_EXPLORATORY=1` labels an exploratory hardware-characterization run,
   which cannot qualify an artifact for production.
8. **Fixed.** `devices.py`/`zones.yaml.example` use `tamper`.
9. **Fixed with the DSC command slice:** the bypass switch command is no longer retained;
   stale retained ON deliveries from the prior discovery payload are rejected. Panel and chime
   callbacks also reject retained physical commands.

## Phase 3 — config — done

Zones load from a YAML file (`ADMQTT_DEVICES_FILE`, default `zones.yaml`; schema in
`zones.yaml.example`; loader `Devices.load_devices`, parity-tested against the legacy
form). `exec` of `devices.py` remains only as a loudly deprecated fallback when no YAML
file exists — remove it next release. `ADMQTT_*` env vars stay for everything else.

## Phase 4 — packaging/deployment — code done, release pending

- **Done.** Dockerfile: `python:3.13-slim`, non-root `USER`, heartbeat-file `HEALTHCHECK`
  (fed by the G14 heartbeat), `.dockerignore`. The private alarmdecoder pin installs via a
  BuildKit secret (`--secret id=git_token`); a release wheel COPY is the documented
  fallback.
- **Done.** `docker-compose.yml` reference stack (bridge + mosquitto,
  `mosquitto.conf.example`).
- **Partially done.** Version is 2.0.0 (`ad_mqtt/version.py`, single source; discovery
  `sw_version` reads it). The immutable release tag still requires independent review and
  the hardware canary — no deployable revision exists yet.
- **Done.** CI: `docker-image.yml` lints (ruff) and runs the suite, then builds and pushes
  `ghcr.io/kars85/ad-mqtt` on main; needs an `ALARMDECODER_TOKEN` repo secret (read access
  to kars85/alarmdecoder). CodeQL bumped to v3. `pyproject.toml` replaces `setup.py`.
- **Open.** Expand tests with recorded KPM/LRR/EXP fixtures from the shadow run and
  complete Bridge event→topic expectations.
- **Done.** Stale ceremony deleted: `.bumpversion.cfg`, `notes.txt`, unused `run()` param,
  the `Config.alarm.port` typo (now 10000), `Client._fileno`, `setup.py`,
  `bump2version`.

## Skipped

- ser2sock TLS client support — ser2sock stays plaintext on a trusted VLAN (webapp cert
  machinery retires with it). Revisit if the bridge VLAN can't be isolated.
- HA add-on packaging — running compose in the Proxmox VM, not HA Supervisor.
