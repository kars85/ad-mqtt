
[![Docker Image CI](https://github.com/kars85/ad-mqtt/actions/workflows/docker-image.yml/badge.svg)](https://github.com/kars85/ad-mqtt/actions/workflows/docker-image.yml)
[![codeql](https://github.com/kars85/ad-mqtt/actions/workflows/codeql.yml/badge.svg)](https://github.com/kars85/ad-mqtt/actions/workflows/codeql.yml)

#### AlarmDecoder to MQTT Bridge (ad-mqtt)

Simple Python application to interface [AlarmDecoder](https://github.com/nutechsoftware/alarmdecoder) with a [MQTT Broker](https://en.wikipedia.org/wiki/MQTT).

Designed to work with [Home Assistant](https://www.home-assistant.io/).  
Uses MQTT discovery to create sensors for all zones and the alarm panel
automatically.  Times are passed in the MQTT messages and retained so they
can be used to create real "last changed" time sensors if desired in HASS.

##### Direct python execution

There is no deployable DSC revision until this work is committed, reviewed, and assigned an
approved immutable commit or tag. Do not clone the default branch for a cutover, and do not
edit tracked source after approval. The private AlarmDecoder dependency requires GitHub
credentials. The tree now reports version `2.0.0`, but only a reviewed immutable tag is a
rollout identity; historical `0.3.2` artifacts must never be used for DSC commands.

Zones are configured in a YAML file (see `zones.yaml.example`; path override via
`ADMQTT_DEVICES_FILE`). The legacy executable `devices.py` is deprecated and read only
when no YAML file exists.

The remaining release, shadow, hardware-canary, rollback, and production steps are tracked in
[`docs/dsc-cutover-handoff.md`](docs/dsc-cutover-handoff.md).

```bash
git status --porcelain
# The command above must print nothing.
git rev-parse HEAD
# Compare the result with the revision recorded in the approved rollout.
git describe --always --dirty
# The description must not end in "-dirty".

APPROVED_SHA=REPLACE_WITH_APPROVED_FULL_SHA
# Build the revision-specific venv and source bundle exactly as specified in
# docs/dsc-cutover-handoff.md. Keep devices.py outside the reviewed checkout.
cd /etc/ad-mqtt-site
/opt/ad-mqtt/venvs/"$APPROVED_SHA"/bin/python \
  /opt/ad-mqtt/releases/"$APPROVED_SHA"/run.py
```

##### Direct Docker Execution

The Dockerfile builds a non-root `python:3.13-slim` image with a heartbeat-file
`HEALTHCHECK`. Authenticate the private AlarmDecoder pin with a BuildKit secret:

```bash
docker build --secret id=git_token,src=.git-token -t ad-mqtt .
```

No published container image contains this DSC safety work yet; a cutover artifact must be
an image built from the approved immutable tag through CI. Never use the historical
`rgriffogoes/ad-mqtt:latest` image for DSC commands.

##### Docker Compose

`docker-compose.yml` is a reference stack (bridge + mosquitto). Copy
`zones.yaml.example` to `zones.yaml`, `mosquitto.conf.example` to `mosquitto.conf`, put a
read token for the AlarmDecoder repo in `.git-token`, then `docker compose up -d`. The
same cutover restrictions apply: commands stay disabled until the handoff gates pass.

##### DSC command safety

Physical panel writes are disabled by default. With `ADMQTT_COMMANDS_ENABLED=false`, the
bridge does not subscribe to panel, chime, or bypass command topics, and its ser2sock
transport blocks every outbound panel key except the `C`/`V` configuration and version
queries. Home Assistant controls may still be discovered, but they intentionally do nothing.
Use an isolated broker for the read-only shadow so this process's retained state publications
cannot collide with the legacy bridge.

The bridge selects DSC or Ademco Vista command bytes only after the AlarmDecoder device
reports its `CONFIG` mode. DSC commands additionally require a fresh keypad message with a
matching panel marker and an appropriate known state. Client considers an authorized segment
transmitted only after the local socket accepts every byte. The bridge then waits for the
expected successful per-key `!Sending...done` reports. DSC actions and chime toggles also wait
for a later authoritative keypad frame; Vista actions release after all expected send reports.
AlarmDecoder's automatic keypad writes are blocked even in enabled mode. Partial, failed, or
otherwise ambiguous writes stay locked across a ser2sock reconnect.

The AD2 reports do not contain correlation tokens. The bridge's FIFO accounting is valid only
when this process is the exclusive AD2 writer. Stop the legacy webapp and every other
ser2sock/serial writer for the canary. Confirm the deployed AD2 firmware's report cardinality,
including DSC S4/S5 special-key sequences; a missing or rejected report intentionally locks
further remote operations.

Complete this preflight while commands remain disabled:

- the exact DSC model and whether bypass zone entry uses the implemented two-digit 1–64
  format;
- `MODE=D`, Quick Arm/function-key behavior, and that "Code Required for Bypass" is disabled;
- the intended `*9` no-entry/night behavior;
- place any monitored alarm account and dispatch workflow in test mode; and
- stop the legacy webapp and every other AD2/ser2sock writer, plus Home Assistant automations
  and other live MQTT publishers, then clear retained values on all three command topics.

Do not enable commands from a shortened README recipe. Follow the complete handoff, including
the persistent-session replay fix, broker/session purge, exclusive-writer proof, restricted
alarm-code file, exact revision-specific artifact, and no-clobber one-shot unlock procedure. An
environment-variable alarm code is permitted only for a labeled exploratory hardware run and
does not qualify an artifact for production. The runner atomically renames the unlock file to
`.consumed`; never recreate it as a blind retry. Test chime, bypass, stay/disarm, night/disarm,
and away/disarm one path at a time, returning to `ADMQTT_COMMANDS_ENABLED=false` on any mismatch.

The bypass request is one-shot and session-local: bypass ON and its arm request must occur in
the same healthy MQTT session. It is cleared on broker or ser2sock reconnect, mode change,
an authenticated arm attempt (including a state/transport rejection), or successful send. Its
discovery command is no longer retained, and retained
`alarm/panel/bypass/set=ON` messages left by older discovery data are ignored. Clear that old
retained broker topic during rollout. Also clear any retained values on `alarm/panel/set` and
`alarm/panel/chime/set`; all three physical command callbacks reject retained deliveries.

##### Changelog

###### Version: 2.0.0 (unreleased)
 - Replaced the insteon-mqtt fork transport with asyncio + paho-mqtt 2.x
   (`clean_session=True`: offline commands are dropped by design, never replayed)
 - MQTT TLS settings (`ADMQTT_MQTT_CA_CERT`/`CERTFILE`/`KEYFILE`) now actually apply
 - Boolean env vars (`ADMQTT_RESTORE_ON_STARTUP`, `ADMQTT_LOG_SCREEN`) parse `"false"` correctly
 - Panel state publishes even for unconfigured zones; alarm restore keeps the armed state;
   `alarm/panel/faulted` is now published
 - Restricted alarm-code file interface (`ADMQTT_ALARM_CODE_FILE`, handoff G15)
 - Payload-free logging (handoff G1) and heartbeat/in-flight monitoring (handoff G14)
 - YAML zone configuration (`zones.yaml`), deprecating executable `devices.py`
 - `pyproject.toml` packaging, GHCR CI with lint + tests, non-root slim Docker image,
   reference compose stack

###### Version: 0.3.2
 - updating codebase from original (TD22057) with minor logging fixes, improved Device attributes and versioning
 - using insteon-mqtt with fixed Paho client to avoid Paho 2 breaking changes (f1d094)
 
###### Version: 0.3.1
 - Adding Device attribute in discovery (sn3ak)
 - Adding codeql in repo workflow

###### Version: 0.3.0
 - Environment variables for major configurations
 - External zone config file
 - Dockerfile for containerized execution
 - Github workflow to build and push docker image
 - Updating README to include further instruction, descripiton and changelog

###### Version: 0.2.3
 - Initial changelog recording
