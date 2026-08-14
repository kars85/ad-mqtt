# ad-mqtt — modernization status and plan

Status reviewed 2026-08-13 against the current source tree. Decision (see
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

### Remaining dependency rewrite

Replace `insteon-mqtt` (personal-fork HEAD `f1d094/insteon-mqtt_with_paho-mqtt-1.6.1`,
unpinned, exists only to pin paho 1.6.1) with **asyncio + paho-mqtt ≥2.x** (or `aiomqtt`).
Only three symbols are used today: `network.Link` (`Client.Client`), `network.Mqtt`
(`ad_mqtt.run.run`), and `network.poll.Manager` (`ad_mqtt.run.run`). `Client.py` already
hand-rolls the whole TCP socket —
an `asyncio.open_connection` + `readline()` loop replaces it in fewer lines. Also removes
`pyyaml`/`Jinja2` (never imported here — transitive insteon-mqtt needs) and the invalid
`git+https://...#egg=` line that breaks `pip install .` (setup.py:20).

The AlarmDecoder pin (`1.14.0`) is done; pin the supported paho major as part of
this rewrite. The public wiring migration is already complete; do not replace it with
`AlarmDecoder.open()` because the current `Client` does not implement the library Device
open API.

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

1. TLS config silently ignored — nested `Data` attrs never survive `cfg.mqtt.__dict__`
   (`Config.py:25-29` + `ad_mqtt/run.py:48`): setting `ADMQTT_MQTT_CA_CERT` does nothing.
   Rewrite config plumbing (falls out of Phase 1).
2. `bool(os.getenv(X, False))` at `run.py:14,29` — `"false"` evaluates True. Parse properly.
3. **Fixed in the compatibility slice:** import and use `RotatingFileHandler` directly
   instead of relying on an unimported `logging.handlers` submodule.
4. `alarm/panel/faulted` is discovered but never published — the entity remains unknown
   in HA. Publish it or drop it.
5. Alarm on an unconfigured zone publishes nothing because `Bridge.publish()` skips the
   unknown zone, swallowing the `triggered` panel state. Panel state must publish even for
   unknown zones.
6. `on_alarm_restored` forces `"disarmed"` even when some panels retain an armed state.
   Resolve this separately with captured panel frames; it is not part of command encoding.
7. Alarm code hygiene: Bridge and Client no longer log keypad JSON, rejected codes, or
   generated key sequences; the runner no longer defaults to `"1234"` and refuses enabled
   commands without an explicit code. The restricted file/secret interface in handoff G15 is
   required before a release-qualifying canary; environment input is exploratory-only.
8. `devices.py:14` `device_class="tampler"` → `tamper`.
9. **Fixed with the DSC command slice:** the bypass switch command is no longer retained;
   stale retained ON deliveries from the prior discovery payload are rejected. Panel and chime
   callbacks also reject retained physical commands.

## Phase 3 — config

Replace `exec(open("devices.py").read())` (`run.py:44`) with a YAML zone file parsed by
stdlib-adjacent means (pyyaml is fine once it's a *real* dependency). Keeps the bind-mount
workflow, drops arbitrary code execution and the cwd dependency. Keep `ADMQTT_*` env vars for
everything else.

## Phase 4 — packaging/deployment (compose stack in Proxmox VM)

- Dockerfile: `python:3.13-slim`, non-root `USER`, `HEALTHCHECK` (e.g. MQTT availability
  topic freshness), `.dockerignore`. Today: `python:3.10.7` full image, root, no healthcheck.
  The alarmdecoder pin is a private git dependency: the image build needs git plus auth
  (e.g. a build secret token) or the wheel handed in from the GitHub release instead.
- Ship a real `docker-compose.yml` (bridge + mosquitto reference). The README requires the host
  venv because no published image contains this revision and the current Dockerfile cannot
  authenticate the private AlarmDecoder pin.
- Assign a new application version and immutable release tag for the guarded build; discovery
  still reports the historical `0.3.2`, so that value must not be used to identify a cutover
  artifact.
- CI: fix `.github/workflows/docker-image.yml` — currently pushes to rgriffogoes' Docker Hub
  with secrets this fork doesn't have (fails every push), deprecated action majors,
  `::set-output`. Retarget to GHCR under kars85, bump actions, add lint step (ruff), and an
  actual test run. CodeQL v2 → v3.
- Tests: the focused AlarmDecoder consumer contract now exists, including command→wire,
  reconnect, CONFIG/KPM ordering, mode-disagreement, and duplicate-delivery assertions.
  Expand it with recorded KPM/LRR/EXP fixtures and complete Bridge event→topic expectations;
  then run it in CI against the pinned release.
- Delete stale ceremony: `.bumpversion.cfg`, `notes.txt`, unused `run()` param
  (`ad_mqtt.run.run`), the `Config.alarm.port` default typo, and `Client._fileno`.

## Skipped

- ser2sock TLS client support — ser2sock stays plaintext on a trusted VLAN (webapp cert
  machinery retires with it). Revisit if the bridge VLAN can't be isolated.
- HA add-on packaging — running compose in the Proxmox VM, not HA Supervisor.
