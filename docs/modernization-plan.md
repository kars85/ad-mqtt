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

- MQTT topics (`ad_mqtt/Bridge.py:25-39`) and HA discovery payloads/`unique_id`s
  (`ad_mqtt/Discovery.py`) — retained in the broker and mirrored as HA entities. Renames
  require a migration step (clear retained topics, let discovery recreate entities).
- `ADMQTT_*` env-var config names (`run.py:11-41`) — keep working for cutover.

## Phase 1 — dependency rescue (the load-bearing fix)

### AlarmDecoder compatibility slice

- [x] Use the library's public, idempotent `AlarmDecoder.wire_events()` while retaining
  ad-mqtt's poll-manager-owned device lifecycle.
- [x] Add `tests/test_alarmdecoder_contract.py`, covering the exact 21 Bridge event
  subscriptions and callback signatures, a raw keypad `Message` flow, and sequential
  `RFMessage` flow through the real decoder into an in-memory MQTT fake.
- [x] Keep these tests independent of live MQTT/ser2sock and the legacy insteon-mqtt
  transport; they use standard-library `unittest` and add no dependency.
- [x] Pin `alarmdecoder` to the immutable modernization release once it is published.
  Do not invent or pin an unpublished version. (Pinned in `requirements.txt` to
  `alarmdecoder @ git+https://github.com/kars85/alarmdecoder@1.14.0`, commit
  `641d46b`; the 4 contract tests pass against the installed 1.14.0 wheel, not the
  sibling path.)

Run the compatibility slice against the pinned, installed distribution
(`pip install -r requirements.txt` in a venv, then `python3 -m unittest discover -s
tests -v`). The sibling-tree form remains a development convenience only:

```bash
PYTHONPATH=../alarmdecoder python3 -m unittest discover -s tests -v
```

### Remaining dependency rewrite

Replace `insteon-mqtt` (personal-fork HEAD `f1d094/insteon-mqtt_with_paho-mqtt-1.6.1`,
unpinned, exists only to pin paho 1.6.1) with **asyncio + paho-mqtt ≥2.x** (or `aiomqtt`).
Only three symbols are used today: `network.Link` (Client.py:11), `network.Mqtt` (run.py:46),
`network.poll.Manager` (run.py:53). `Client.py` already hand-rolls the whole TCP socket —
an `asyncio.open_connection` + `readline()` loop replaces it in fewer lines. Also removes
`pyyaml`/`Jinja2` (never imported here — transitive insteon-mqtt needs) and the invalid
`git+https://...#egg=` line that breaks `pip install .` (setup.py:20).

The AlarmDecoder pin (`1.14.0`) is done; pin the supported paho major as part of
this rewrite. The public wiring migration is already complete; do not replace it with
`AlarmDecoder.open()` because the current `Client` does not implement the library Device
open API.

## Phase 2 — bug fixes (from audit, keep behavior otherwise)

0. **DSC panel command sequences (blocking for cutover).** The deployed panel is DSC
   hardware, but every command path sends Ademco Vista key sequences: `code+2`/
   `code+3`/`code+7`/`code+1` arm/disarm (`Bridge.py:77-84`), `code+6#` bypass prefix
   (`Bridge.py:75`), `code+9` chime toggle (`Bridge.py:109`). DSC PowerSeries uses
   different sequences (disarm is the code alone, `*1<zones>#` bypass, `*4` chime,
   stay/away via function keys). Branch the key sequences on the decoder's panel mode
   (`alarmdecoder.panels.DSC`, populated from the AD2 `CONFIG` response) and verify
   each against the real panel during the one-command-path-at-a-time canary. Reading
   state (events, zone faults) is unaffected; this blocks only command paths.

1. TLS config silently ignored — nested `Data` attrs never survive `cfg.mqtt.__dict__`
   (`Config.py:25-29` + `ad_mqtt/run.py:48`): setting `ADMQTT_MQTT_CA_CERT` does nothing.
   Rewrite config plumbing (falls out of Phase 1).
2. `bool(os.getenv(X, False))` at `run.py:14,29` — `"false"` evaluates True. Parse properly.
3. **Fixed in the compatibility slice:** import and use `RotatingFileHandler` directly
   instead of relying on an unimported `logging.handlers` submodule.
4. `alarm/panel/faulted` discovered but never published (`Bridge.py:28`,
   `Discovery.py:90,92`) — entity permanently unknown in HA. Publish it or drop it.
5. Alarm on an unconfigured zone publishes nothing — `Bridge.py:130-135` early-return
   swallows the `triggered` panel state (`Bridge.py:177`). Panel state must publish even for
   unknown zones.
6. `on_alarm_restored` forces `"disarmed"` (`Bridge.py:181`) even while still armed.
7. Alarm code hygiene: stop logging keypad sequences at INFO (`Bridge.py:89`), stop
   defaulting to `"1234"` (`run.py:41`); support docker secret / file-based code.
8. `devices.py:14` `device_class="tampler"` → `tamper`.
9. Bypass switch retained command topic (`Discovery.py:138`) — drop retain on command topics.

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
- Ship a real `docker-compose.yml` (bridge + mosquitto reference; README example currently
  points at `rgriffogoes/ad-mqtt:latest`).
- CI: fix `.github/workflows/docker-image.yml` — currently pushes to rgriffogoes' Docker Hub
  with secrets this fork doesn't have (fails every push), deprecated action majors,
  `::set-output`. Retarget to GHCR under kars85, bump actions, add lint step (ruff), and an
  actual test run. CodeQL v2 → v3.
- Tests: the focused AlarmDecoder consumer contract now exists. Expand it with recorded
  KPM/LRR/EXP fixtures, command→wire assertions, reconnect/lifecycle coverage, and complete
  Bridge event→topic expectations; then run it in CI against the pinned release.
- Delete stale ceremony: `.bumpversion.cfg`, `notes.txt`, unused `run()` param
  (`ad_mqtt/run.py:33`), `Config.py:13` port-`1000` default typo, `Client.py:199` dead
  `_fileno`.

## Skipped

- ser2sock TLS client support — ser2sock stays plaintext on a trusted VLAN (webapp cert
  machinery retires with it). Revisit if the bridge VLAN can't be isolated.
- HA add-on packaging — running compose in the Proxmox VM, not HA Supervisor.
