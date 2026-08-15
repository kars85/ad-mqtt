# DSC command cutover and modernization handoff

Last reviewed: 2026-08-14

This document hands off the work required to turn the guarded DSC command implementation on
this branch into a reviewed canary, then a production deployment. It also records
the broader v2 work that is not required for the first attended canary.

The short version is: **the command implementation is locally tested, but there is no
deployable revision yet and physical commands must remain disabled.**

## Current snapshot

| Item | State at handoff |
|---|---|
| Branch | `alarmdecoder-v2-contract` |
| Guarded-slice baseline | `2bcec3fe63db21df473100832ec5edc0924723bd`; the implementation is in the later commit containing this handoff |
| Candidate identity | No RC selected; record the eventual protected-main SHA and immutable RC tag |
| Application version | Historical `0.3.2`; not a valid cutover identity |
| AlarmDecoder | Private Git tag `1.14.0`, tag commit `b4ac7293b254e1cd214247b6323212698c7e7619` |
| insteon-mqtt | Mutable VCS dependency; review used commit `c1404056d7496dde227f6797025ef66d00c460d3` |
| Local verification | 44 tests pass against an installed AlarmDecoder 1.14.0 and sibling source; Flake8, `compileall`, `pip check`, and `git diff --check` pass |
| CI/release | No successful Actions run, PR, release, remote tag, or published image for this work |
| Hardware proof | Not performed |

The current [README](../README.md) explains the implemented command gates. The broader backlog
is in the [modernization plan](modernization-plan.md). The cross-repository appliance procedure
is in `../alarmdecoder/docs/appliance-validation.md`, but it must be updated as described below
before it is used as rollout authority.

## Non-negotiable stop rules

Keep `ADMQTT_COMMANDS_ENABLED=false` until every gate marked **before canary** is complete.

- Do not deploy from a dirty or uncommitted checkout, the default branch, `0.3.2`, or
  `rgriffogoes/ad-mqtt:latest`.
- Do not enable commands while the legacy webapp, legacy bridge, a raw AD2/ser2sock terminal,
  or any other panel writer is active. `!Sending` responses have no writer or command
  correlation. The separate MQTT operator console is required and is not a panel writer.
- Do not run the enabled canary on an MQTT session that may contain persistent subscriptions
  or queued commands at any QoS.
- Do not retry an ambiguous, partial, failed, or timed-out command. Inspect and recover at the
  physical keypad, then return to commands-disabled operation.
- Do not automatically recreate `ADMQTT_COMMANDS_UNLOCK_FILE`. It is intentionally one-shot.
- Do not capture plaintext MQTT or ser2sock traffic, enable shell tracing, or store an alarm
  code in the evidence bundle.
- Do not exercise alarm, fire, panic, medical, tamper, or other life-safety paths as part of
  the command canary. An alarm during the canary is an immediate abort.

## Definition of done

There are three separate milestones. Do not collapse them.

1. **Immutable release candidate:** the code and dependencies are fixed, reviewed, committed,
   reproducible, and installed from a clean artifact.
2. **Attended hardware canary:** the candidate passes the disabled shadow and every supervised
   DSC command path on the deployed panel with the new bridge as the sole writer.
3. **Production release:** the remaining telemetry, MQTT-session, secret, CI, and deployment
   gates are resolved; the exact hardware-tested tree is tagged and deployed with rollback and
   monitoring.

Any code or dependency change after hardware testing creates a new candidate. Repeat the
affected validation; never move an existing tag to the new commit.

## Gate matrix

| ID | Remaining item | Required before | Acceptance evidence |
|---|---|---|---|
| G1 | Make all runtime logs and send-progress evidence payload-free | Canary | Integration tests prove codes, keypad text, and raw lines are absent at INFO/DEBUG |
| G2 | Pin/hash-lock both VCS dependencies and all build/test inputs | Canary | Builder provenance maps reviewed commits to wheel hashes; `pip check` passes |
| G3 | Commit, review, and assign an immutable RC identity | Canary | Clean tree, approved PR, full SHA, checksums, green checks |
| G4 | Use a fresh isolated broker plus reviewed queue/ACL settings | Canary | Broker config, session inventory, and empty queue/retained-command proof |
| G5 | Complete read-only observation and 24-hour shadow | Canary | Signed comparison table with no unexplained mismatch |
| G6 | Confirm panel model, programming, zone format, and command semantics | Canary | Physical-keypad worksheet signed by the operator |
| G7 | Establish exclusive AD2 writer ownership and monitoring test mode | Canary | Client/process inventories plus monitoring ticket/window |
| G8 | Capture real AD2 send-report cardinality and run all command paths | Production | Per-action evidence table and operator/rollback sign-off |
| G9 | Fix persistent MQTT command-session replay | Canary | Offline QoS 0/QoS 1 publish/reconnect tests produce zero callback/write |
| G10 | Fix missing/false alarm-state behavior | Production | Recorded DSC fixtures and regression tests pass |
| G11 | Resolve MQTT and ser2sock transport security for the selected environment | Shadow/canary | Verified TLS where implemented, or approved plaintext only on the isolated VLAN |
| G12 | Produce the chosen production artifact and deployment automation | Production | Digest-pinned install, health check, rollback rehearsal |
| G13 | Synchronize cross-repository rollout documents | Canary | Joint documentation review has no unsafe current guidance |
| G14 | Add actionable liveness and in-flight-age monitoring | Production | Heartbeat/external liveness and non-mutating ACK-age alerts are tested |
| G15 | Move the alarm code to a restricted secret/file interface | Qualifying canary | Permission, missing-secret, process-environment, and log tests pass |

Complete every known production code gate before the canary intended to qualify a release. If
an earlier attended run is needed to learn panel semantics or ACK cardinality while G10 or G14
remains open, label it an **exploratory hardware-characterization canary**. G9 is required even
for that attended run. Any subsequent code, dependency, config-loader, Python, container, or
runtime-artifact change creates a new RC and requires the relevant shadow plus the full attended
command canary again.

## 1. Close the pre-canary code and security gaps

### 1.1 Make runtime logging and send evidence safe

Several current log paths are unsuitable for a retained canary record:

- insteon-mqtt logs `message.topic` and `message.payload` at INFO in
  `network/Mqtt.py::_on_message`;
- `Client.parse_read_buf()` logs each raw ser2sock line at DEBUG;
- `Bridge.on_message()` logs the full KPM representation at DEBUG; and
- `Bridge.publish()` can log keypad/panel message text at INFO.

Paho topic callbacks bypass the wrapper's generic `_on_message` when a registered filter
matches. An ordinary enabled command therefore uses the redacted Bridge callback. The generic
wrapper leak is still reachable for an unmatched topic or a command restored through an old
persistent subscription when no filtered callback exists. Remove the leak unconditionally; do
not describe a direct `_on_message` unit call as the normal command route.

Before enabling commands:

1. Replace every raw payload/frame/KPM log with structured, non-sensitive metadata. Do not log
   payload bytes, decoded command JSON, keypad text, raw bitfields, or generated keys.
2. Add structured INFO telemetry for each expected send result: operation type, success/failure,
   ordinal/expected count, remaining count, and age. It must never include a code or key bytes.
3. Test both Paho routes: a matched filtered command callback and the generic/unmatched restored-
   session fallback. Use unique sentinel code and keypad-text values at INFO and DEBUG.
4. Assert every sentinel is absent from every logger used by the process, while safe structured
   send progress remains sufficient to verify response cardinality.
5. Review any historical enabled-run logs as secret-bearing data. Restrict them and follow the
   site's credential/log-retention procedure; rotate the alarm credential if exposure is
   plausible.

Pass condition: complete matched and fallback message paths plus a panel command can run while
tests prove that code, keys, raw frames, and keypad text are absent. The retained INFO log must
still show payload-free CONFIG/KPM summaries and send-result progress/counts. Do not retain
DEBUG output during the canary.

### 1.2 Eliminate queued MQTT command replay

**Code status (2026-08-14):** the transport rewrite (`ad_mqtt/Mqtt.py`, paho-mqtt 2.x)
constructs the client with `clean_session=True`; a wrapper unit test is the tripwire against
regressing this. Consequence by design: commands published while the bridge is offline are
**dropped** by the broker, never queued and replayed — an operator must not expect an offline
disarm to execute on reconnect. The legacy wrapper used client ID `ad-mqtt` with
`clean_session=False`, and clearing retained messages does **not** remove that historical
broker-side persistent session, its subscriptions, or offline QoS 1 messages — the broker
purge steps below remain mandatory.

The remaining G9 acceptance work is the live-broker integration evidence (unit tests cannot
prove broker behavior). Add broker integration tests for both QoS 0 and QoS 1:

1. connect and reach an authoritative, command-ready state;
2. disconnect the bridge;
3. publish an arm action while it is offline;
4. reconnect; and
5. assert that no command callback or panel write occurs.

Also test that commands-disabled startup does not inherit old command subscriptions. MQTT 3.1.1
permits a broker to store matching QoS 0 messages for a persistent session, so QoS 0 alone is
not a no-queue guarantee.

After the code fix, retain all of these canary defenses:

- a genuinely fresh isolated broker or a broker/session proven never to have hosted that
  client ID;
- an explicit broker-administration check showing no persistent `ad-mqtt` session or queued
  messages;
- ACLs installed before the candidate connects; and
- one manual publisher using QoS 0 and never retain;
- broker-specific proof that QoS 0 offline queuing is disabled; and
- immediate abort, publisher-ACL revocation, and session purge on any disconnect.

Broker session deletion remains a rollout step even after the code fix.

### 1.3 Make dependencies reproducible

For a network source install, pin AlarmDecoder directly to the release commit rather than the
movable tag, while documenting that the commit is release 1.14.0:

```text
alarmdecoder @ git+https://github.com/kars85/alarmdecoder@b4ac7293b254e1cd214247b6323212698c7e7619
```

The insteon-mqtt requirement currently follows mutable HEAD. For the interim candidate, replace
it with valid PEP 508 syntax pinned to the reviewed commit, for example:

```text
insteon-mqtt @ git+https://github.com/f1d094/insteon-mqtt_with_paho-mqtt-1.6.1@c1404056d7496dde227f6797025ef66d00c460d3
```

Then build a wheelhouse and reviewed constraints/lock manifests for the exact target Python and
Linux architecture. Pin build tools and test tools as well as runtime dependencies. Record
SHA-256 hashes. Do not embed a GitHub token in a URL, wheel, container layer, log, or artifact.
Use a least-privilege GitHub App installation token, an equivalently scoped fine-grained token,
or a prebuilt approved AlarmDecoder wheel.

The authenticated builder proof must include the VCS provenance before wheels are created:

```bash
python - <<'PY'
from importlib import metadata

for name in ("alarmdecoder", "insteon-mqtt"):
    dist = metadata.distribution(name)
    print(name, dist.version)
    print(dist.read_text("direct_url.json"))
PY
```

The AlarmDecoder entry must resolve to the 1.14.0 commit above, and insteon-mqtt must resolve to
the reviewed commit. Bind those source commits to the resulting wheel SHA-256 values in a signed
or otherwise approved provenance manifest. An offline target install normally records a local
wheel URL instead of the original VCS `direct_url.json`; validate its wheel hashes and installed
versions against the manifest rather than requiring VCS metadata on the target. Keep private
AlarmDecoder wheels in restricted artifact storage or private registry. Do not attach them to a
public ad-mqtt release or public image without an explicit disclosure/licensing decision.

Replace the legacy dependency entirely during the v2 transport rewrite; the commit pin is an
interim reproducibility measure.

### 1.4 Fix command-adjacent telemetry defects

Resolve these before production, preferably before the first candidate so hardware evidence is
not attached to a tree that must immediately change:

- An alarm on an unconfigured zone calls `_update_panel_status(..., zone=unknown)` and can lose
  the `triggered` MQTT publication when zone lookup fails. Publish the panel state independently
  of optional zone metadata and add an unknown-zone regression.
- `on_alarm_restored` always publishes `disarmed`. Capture real DSC armed-to-alarm-to-restore
  frames, determine the authoritative post-alarm state, and add regressions for alarm frames
  both with and without retained armed bits. Do not guess the state transition.

Until these are fixed, do not intentionally trigger an alarm during validation.

### 1.5 Close environment-specific blockers

- MQTT TLS environment values are currently lost by nested config serialization. If the
  candidate broker requires TLS, fix the config plumbing and verify certificate, hostname, and
  failure behavior before use. Otherwise the canary broker must be isolated and trusted.
- The current `ad_mqtt.Client` is plaintext-only even though the sibling observation probe can
  use TLS. Before the shadow, confirm the actual ser2sock listener mode. If it requires TLS, the
  candidate is incompatible: either implement/test TLS in Client or provision an explicitly
  approved plaintext listener on the isolated VLAN. A successful TLS probe does not prove this
  application can connect.
- Parse generic boolean environment variables explicitly; strings such as `"false"` currently
  become true for restore/log-screen settings.
- Keep the attended canary code out of shell history. Implement an
  `ADMQTT_ALARM_CODE_FILE`-style interface before the release-qualifying canary. When commands
  are enabled, fail startup unless an explicit code file is supplied; never fall back to the
  historical `1234`. Open only a regular, non-symlink file with an approved owner and mode,
  reject missing/empty/insecure input, and keep the value out of argv and exported process
  environment. Redact every log path and document credential rotation. Test the permission,
  missing-file, default-code, process-environment, and logging failure cases.
- Publish or remove the permanently unknown `alarm/panel/faulted` discovery entity.
- Correct `device_class="tampler"` to `tamper`.

## 2. Create an immutable release candidate

### 2.1 Choose identity and scope

Treat this as the guarded 0.4 line rather than the unfinished v2 rewrite. One safe identity
scheme is to set the source version to `0.4.0`, mark the candidate commit with
`v0.4.0-rc.1`, and add `v0.4.0` to that **same commit** only after hardware evidence passes.
This avoids changing the tested source merely to remove an `rc` suffix. The maintainer may
choose another scheme, but must not reuse `0.3.2` or claim that a different final commit is the
hardware-tested tree.

Update together:

- `setup.py`;
- `ad_mqtt/version.py` (and therefore discovery `sw_version`);
- README changelog/release notes; and
- the artifact manifest.

Do not use `.bumpversion.cfg` as release authority; it commits automatically, does not tag, and
is already slated for removal.

### 2.2 Run clean installed-artifact validation

Authenticate to the private dependency without putting a token in repository configuration.
In the authenticated builder, first build the two VCS wheels from their fixed commits and
capture their source provenance. Download/build every transitive runtime, build, and test wheel
for the selected Python/Linux target. Then generate reviewed `name==version --hash=...` files
for build tools, runtime, and tests. Those offline install locks must contain wheel-installable
requirements only—no VCS URLs.

Select the approved Python minor/patch explicitly and validate entirely from that wheelhouse:

```bash
PYTHON=/approved/path/to/python3
WHEELHOUSE=/approved/path/to/wheelhouse
VERIFY_ROOT=$(mktemp -d /tmp/ad-mqtt-rc-verify.XXXXXX)
"$PYTHON" -m venv "$VERIFY_ROOT/venv"
"$VERIFY_ROOT/venv/bin/python" -m pip install \
  --no-index --find-links "$WHEELHOUSE" --require-hashes \
  -r release/build-tools.lock
"$VERIFY_ROOT/venv/bin/python" -m pip install \
  --no-index --find-links "$WHEELHOUSE" --require-hashes \
  -r release/runtime.lock -r release/test.lock

"$VERIFY_ROOT/venv/bin/python" -m unittest discover -s tests -v
"$VERIFY_ROOT/venv/bin/flake8" ad_mqtt tests run.py
"$VERIFY_ROOT/venv/bin/python" -m compileall -q ad_mqtt tests run.py
"$VERIFY_ROOT/venv/bin/python" -m pip check
git diff --check
```

Do not substitute unreviewed latest versions for the locks. The current 44-test suite is
source-relative: it uses its own `REPO_ROOT` and loads Bridge/Client/root-runner files directly.
Run it against the exact archived source bundle and add/assert path checks showing every loaded
application file is under that approved bundle.

Separately build the ad-mqtt wheel, install it with `--no-deps` into a second venv populated from
the same hash-locked wheelhouse, then run an import/version smoke test from a temporary directory
outside the checkout. Assert `Path(ad_mqtt.__file__)` is under that venv. This packaging smoke
does not replace the source-bundle contract suite and is not the current runtime entrypoint.
Record Python, pip, dependency, OS, source-bundle, and wheel hashes with both results.

### 2.3 Commit and review deliberately

1. Review `git diff` by subsystem; do not stage unrelated user work.
2. Stage explicit files and review `git diff --cached`.
3. Commit the code/tests/docs in an auditable sequence or one explicitly reviewed safety
   commit.
4. Push the branch and open a PR against `main`.
5. Require at least one independent approval and passing required checks.
6. Dismiss stale approvals after changes and prohibit force-push/delete on protected release
   refs.
7. Merge the approved PR to protected `main` **before** creating the RC artifact. Build and
   canary the resulting immutable main SHA, not the feature-branch SHA. If the merge strategy
   changes the SHA, record the new tree and rerun the release checks against it.
8. Add the RC tag to that main SHA. Every fix uses a new PR, main SHA, and RC tag.

The repository currently has no branch/tag protections or historical Actions runs. Add required
checks before treating a PR badge as release evidence. Set default workflow permissions to
read-only; grant package write only to the protected release job. Never use
`pull_request_target` to execute untrusted PR code with the private dependency credential.

Before creating an RC artifact, require:

```bash
git status --porcelain
git rev-parse HEAD
git describe --always --dirty
```

The first command must print nothing, the second must equal the approved full SHA, and the third
must not end in `-dirty`.

### 2.4 Repair CI

The current Docker workflow installs dependencies but runs no tests, uses obsolete action
revisions, targets another user's Docker Hub repository, and has no usable release credentials.
CodeQL is also stale.

The PR workflow must:

- install the full runtime and test dependencies from fixed inputs;
- assert both VCS commit IDs;
- run unittest, Flake8, `compileall`, `pip check`, and package install validation;
- run for documentation changes because the runbook is safety-relevant;
- build any container with `push=false` on PRs; and
- use currently supported action code pinned by full commit SHA.

The protected final-tag workflow must promote the already canaried artifact/digest rather than
silently rebuilding it. It may publish only to the selected owner namespace, ideally a private
GHCR package, and attach public-safe checksums, dependency provenance, SBOM/provenance, and
release notes. Keep any private AlarmDecoder wheel or image layer in restricted storage unless
an explicit disclosure/licensing review approves public distribution.

### 2.5 Build the canary artifact

The fastest safe canary artifact is a host-venv bundle, not the current Dockerfile.

1. Start from a fresh detached checkout of the approved RC SHA.
2. Archive the source and build/download the exact wheelhouse in a controlled authenticated
   builder. Capture VCS `direct_url.json` there.
3. Generate and review a provenance/SHA-256 manifest that binds each private/VCS source commit
   to its wheel hash. Generate a separate hash-locked runtime requirements file containing
   wheel-installable `name==version` entries, not VCS URLs.
4. Create `/opt/ad-mqtt/venvs/<full-sha>` with the approved Python, then install through that
   venv's `python -m pip` using `--no-index --find-links /approved/wheelhouse --require-hashes
   -r release/runtime.lock`.
5. Verify manifest/wheel hashes, installed versions, `pip check`, imports, version, and tests
   from outside the checkout. Do not expect target `direct_url.json` to retain VCS provenance.
6. Install the application source bundle under a revision-specific, read-only path such as
   `/opt/ad-mqtt/releases/<full-sha>`. The dependency venv and source bundle together are the
   current host artifact; include ownership/mode/path checks and both trees in its manifest.
7. Keep `devices.py` under `/etc/ad-mqtt-site`; record its checksum without copying secrets or
   site details into the repository.

The current runner prepends its working directory to `sys.path` and executes `devices.py` as
Python. Before canary, require `/etc/ad-mqtt-site` and `devices.py` to be root-owned/restricted,
inventory and checksum every directory entry, and prove there is no shadow `ad_mqtt`,
`alarmdecoder`, `insteon_mqtt`, or other importable module/package there. The audited
`devices.py` must be the only intended executable site file. Replacing this mechanism with a
validated explicit config loader remains a v2 requirement.

Record the previous known-good artifact and exact symlink/service procedure for rollback. Do
not build or run the current `python:3.10.7` root container or any `latest` tag.

## 3. Synchronize cross-repository instructions

Update and review these documents together before the canary. Until that happens, this handoff
supersedes the enabled-command phase in the sibling appliance runbook:

1. `../alarmdecoder/docs/ecosystem.md`
   - Replace the Vista topology with the deployed DSC hardware.
   - Record AlarmDecoder 1.14.0 as released and pinned.
   - Replace unpinned/~21-event language with the exact 23 Bridge subscriptions.
   - Record commands-default-off, the exclusive-writer constraint, and pending canary.
2. `../alarmdecoder/docs/modernization-plan.md`
   - Preserve counts that are explicitly historical evidence for older commits.
   - Add a separate current-status entry for 23 subscriptions, the current contract suite, and
     the ad-mqtt release/canary state.
3. `../alarmdecoder/docs/appliance-validation.md`
   - Require every webapp/AD2/ser2sock writer to be stopped for enabled commands.
   - Add persistent MQTT session purge, all three retained command topics, the one-shot
     unlock, DSC model/programming checks, S4/S5 report capture, and night/disarm.
   - Describe bypass as a one-shot intent paired with an arm, not a standalone panel toggle.
4. `../alarmdecoder-webapp/docs/modernization-plan.md`
   - Remove any claim that `alarm/panel/set` accepts arbitrary raw keypad input.
   - Record the allowed action/chime/bypass surfaces and exclusive-writer shutdown.

Pass condition: no stale `Vista panel`, `unpinned`, `~21`, arbitrary-raw-command, or
running-webapp-during-canary guidance is presented as current. Truthfully labeled historical
counts remain unchanged.

## 4. Preserve rollback and collect site facts

Do this with commands disabled.

Record outside the repository:

- panel model/board/firmware, partition, keypad address, zone list, expanders/relays, RF
  serials/loops, AD2 model/firmware/config, and ser2sock endpoint;
- Pi UART, baud rate, network/VLAN/firewall route, and plaintext/TLS selection;
- MQTT broker/session/topic inventory and Home Assistant entities/automations;
- service names, current process/client inventory, and notification ownership;
- monitoring-provider test procedure, physical operator, and rollback owner; and
- chosen RC SHA, dependency provenance, artifact checksum, and external config checksum.

Preserve and verify:

- appliance SD image or equivalent backup;
- webapp database/configuration;
- ser2sock configuration, certificates if used, network config, service status, and logs;
- retained `alarm/#` and Home Assistant discovery/entity inventory;
- current devices/zone/notification configuration; and
- the old path's ability to observe one ordinary fault and restore.

Do not open `/dev/serial0` while ser2sock owns it.

## 5. Run the zero-command observation and shadow

### 5.1 Low-level probes

Use the sibling AlarmDecoder runbook and probe from the Proxmox VM:

```bash
python ../alarmdecoder/examples/network_appliance_probe.py APPLIANCE_HOST \
  --port 10000 \
  --seconds 30 \
  --transport-only

python ../alarmdecoder/examples/network_appliance_probe.py APPLIANCE_HOST \
  --port 10000 \
  --seconds 300 \
  --output appliance-probe.json
```

The transport-only probe must send nothing. The decoded probe may send only the normal C/V
configuration/version queries. Require a stable connection, configuration/version responses,
parsed frames, no invalid-message burst, and a clean reconnect.

### 5.2 Twenty-four-hour shadow

Run the exact RC with `ADMQTT_COMMANDS_ENABLED=false` against a fresh isolated broker. The
current client ID and topic namespace are not safe for a same-broker shadow. If a shared broker
is required, first add configurable client ID, state/discovery prefix, and session behavior.

Run the old observation path and candidate concurrently for at least 24 hours. Compare:

- stable `CONFIG MODE=D` and matching DSC KPM markers;
- every configured wired-zone fault/restore;
- representative RF loop, battery, and supervision data;
- EXP/REL/LRR/AUI data when the site uses them;
- passive ready, armed-away, armed-home, disarmed, chime, bypass, AC, and battery transitions
  that occur during ordinary use;
- any deliberate arm/disarm exercise only after the monitoring test window and attended
  procedure in section 6.1 are active;
- alarm/restore behavior from already-sanitized captures only; do not deliberately trigger an
  alarm for the shadow while the known restore defect remains;
- availability and reconnect behavior; and
- duplicate/missing publication behavior.

Only C/V may leave the candidate transport. Record a comparison table containing timestamp,
physical action, expected topic/event, old result, candidate result, and mismatch explanation.
Store sanitized fixtures; do not store keypad text or household activity that is not needed for
a regression.

Pass condition: there is no unexplained state, zone, availability, reconnect, or publication
mismatch. Fix discrepancies in a new RC and restart the shadow clock.

## 6. Establish the deployed DSC command contract under test conditions

First inspect panel documentation/programming and record these facts without entering
programming mode, altering settings, or issuing a state-changing keypad sequence:

- the AD2 reports `MODE=D` and controls exactly the intended partition;
- the exact panel model uses two-digit bypass entry for zones 01-64;
- S4/S5 assignments and Quick Arm/function-key configuration;
- the “Code Required for Bypass” setting and its approved security policy;
- the documented `*9 + access code` behavior;
- the selected code is an ordinary user code, not duress, one-time, installer, or master unless
  site policy explicitly requires it;
- exit/entry delays are known; and
- one safe, non-24-hour zone is designated for bypass testing.

If a programming change appears necessary, stop. Have it performed through the site's qualified
panel-maintenance procedure, capture a new baseline, and repeat commands-disabled observation
and the shadow before scheduling a canary. Do not alter panel programming as part of this
runbook.

Useful primary references include the [DSC PowerSeries user manual](https://cms.dsc.com/download.php?id=16783&t=1),
[PowerSeries installation manual](https://www.dsc.com/psp/eu/media/documents/en/installation-manual.pdf),
and [PowerSeries Pro reference](https://www.dsc.com/psp/eu/media/documents/en/PowerSeries_Pro_English_Reference.pdf).
Use the manual for the exact deployed model as authority.

### 6.1 Put the site in test condition before active checks

- Place the monitored account/dispatch workflow into test mode. Record ticket/operator and
  explicit start/end times with rollback buffer.
- Notify occupants and disable downstream, non-life-safety HA automations, locks,
  notifications, or routines that react to alarm state. Never disable the panel siren, local
  annunciation, monitoring transport, or another life-safety signal as a canary convenience.
- Assign a physical-recovery operator with the local code and monitoring contact. The Away plan
  below determines whether that person can remain at a keypad or must exit the protected area.

## 7. Prepare the attended canary

### 7.1 Isolate AD2 ownership

- Before active keypad checks, deny command-topic writes on every production/legacy broker and
  stop HA command automations, scripts, and manual publishers. Preserve the prior ACL baseline,
  but do not restore command authority until rollback/production telemetry is stable.
- Stop the legacy bridge, webapp, terminals, direct-serial tools, and every other AD2/ser2sock
  writer. Keep their artifacts ready for rollback, but not running. Verify that stopping the
  webapp does not stop the independently required ser2sock service.
- Firewall ser2sock so only the canary host can connect. Verify zero clients before candidate
  startup and exactly one afterward.

### 7.2 Isolate MQTT

Install ACLs before the candidate connects:

- candidate: read only the three command topics; write only its approved availability,
  discovery, state, and sensor topics;
- manual publisher: write only the three command topics;
- observer: read only; and
- every other identity: no command-topic writes.

Use distinct client IDs for candidate, operator, and observer. With legacy clients stopped,
inspect and purge command subscriptions/offline queues on every legacy/production broker as well
as any prior `ad-mqtt` session on the canary broker.

With every bridge stopped, clear these retained command topics on both the legacy/production and
canary brokers. First provision a reviewed, root-owned wrapper that invokes `mosquitto_pub` with
the exact administrative broker endpoint, identity, and TLS/authentication settings. It must
accept the remaining arguments literally, avoid logging them, and fail if required credentials
are unavailable. Record its checksum, then use its absolute path so the example fails safely if
site authentication has not been configured:

```bash
ADMIN_PUBLISH=/secure/bin/ad-mqtt-admin-publish
test -x "$ADMIN_PUBLISH" && test ! -L "$ADMIN_PUBLISH" || exit 1
sha256sum "$ADMIN_PUBLISH"
"$ADMIN_PUBLISH" -q 1 -r -n -t alarm/panel/set
"$ADMIN_PUBLISH" -q 1 -r -n -t alarm/panel/chime/set
"$ADMIN_PUBLISH" -q 1 -r -n -t alarm/panel/bypass/set
```

Connect a fresh audit subscriber and prove no non-empty retained command value is delivered.
Do not clear retained state topics. Retained deletion alone is not session-queue deletion.

### 7.3 Prepare restricted evidence

Synchronize bridge, broker, ser2sock, and operator clocks. Select a new run ID, then create a
mode-0700 evidence directory that did not previously exist. Pre-create its application logfile
mode 0600 and point `ADMQTT_LOG_FILE` to it. Capture:

- RC SHA/version, clean-tree proof, dependency provenance, artifact/config checksums;
- panel/AD2 facts and monitoring ticket;
- ACL checksum, retained cleanup, persistent-session purge, and connected-client inventory;
- ser2sock/serial owner inventory;
- structured, payload-free INFO logs containing CONFIG/KPM summaries and `!Sending` progress;
  and
- timestamped keypad/MQTT observations for each action.

Do not use packet capture, shell tracing, or retained DEBUG output. Confirm all G1 logging tests
have passed and verify the INFO sample is payload-free before the canary.

### 7.4 Confirm state-changing keypad semantics under isolation

Only after sections 6.1 and 7.1–7.3 are complete, verify S4/S5, Quick Arm, bypass-code behavior,
and `*9` semantics from the physical keypad using the approved test procedure. Do not change
programming. For Away, everyone must leave the protected detection area unless the recovery
keypad is explicitly verified to be outside all armed coverage. Use a documented exit/re-entry
and local-disarm plan with at least two people and continuous communication.

If observed behavior differs from documentation or the implemented contract, stop, restore the
baseline, and create a new RC; do not continue to MQTT commands.

## 8. Execute the command canary

### 8.1 Use separate bridge, publisher, and observer consoles

Use three restricted consoles with distinct roles. The bridge runs in the foreground in console
1, the only operator publisher runs in console 2, and console 3 tails the restricted log and
observes MQTT state while the keypad operator reports physical state. Do not background the
bridge to reuse its shell.

In bridge console 1, use the exact approved artifact and external site directory. First load the
reviewed non-command connection environment through a root-owned, mode-0600, checksummed file or
equivalent service-manager mechanism. It must define the explicit ser2sock endpoint, canary
broker endpoint, candidate client identity, and authentication. Do not print secret values.

The release-qualifying canary must use the completed G15 secret-file interface. The panel code
must be absent from argv and the process environment. An environment-variable code may be used
only for an explicitly labeled exploratory hardware-characterization run; that run cannot
qualify an artifact for production.

Then audit/archive any prior unlock/`.consumed` evidence. Both paths must be absent. Create the
new unlock with shell noclobber so a stale file makes the operation fail. Before running the
block, verify with `stat` that both secure parent directories and the environment file have the
approved owner/mode and are not group/world writable/readable:

```bash
umask 077
set +x

CANARY_ENV=/secure/config/ad-mqtt-canary.env
test -f "$CANARY_ENV" && test ! -L "$CANARY_ENV" || exit 1
# Inspect owner/mode before sourcing; archive only the checksum, never contents.
stat -c '%U %G %a %n' "$CANARY_ENV" "$(dirname "$CANARY_ENV")"
sha256sum "$CANARY_ENV"
set -a
. "$CANARY_ENV"
set +a

# The environment file must not authorize commands or select a panel-code source by itself.
unset ADMQTT_COMMANDS_ENABLED ADMQTT_COMMANDS_UNLOCK_FILE \
  ADMQTT_ALARM_CODE ADMQTT_ALARM_CODE_FILE
: "${ADMQTT_SOCKET_HOST:?missing reviewed ser2sock host}"
: "${ADMQTT_SOCKET_PORT:?missing reviewed ser2sock port}"
: "${ADMQTT_MQTT_HOST:?missing reviewed MQTT host}"
: "${ADMQTT_MQTT_PORT:?missing reviewed MQTT port}"
: "${ADMQTT_MQTT_CLIENT_ID:?missing unique candidate client ID}"
: "${ADMQTT_MQTT_USERNAME:?missing candidate MQTT identity}"
: "${ADMQTT_MQTT_PASSWORD:?missing candidate MQTT credential}"
printf 'ser2sock=%s:%s mqtt=%s:%s client=%s\n' \
  "$ADMQTT_SOCKET_HOST" "$ADMQTT_SOCKET_PORT" \
  "$ADMQTT_MQTT_HOST" "$ADMQTT_MQTT_PORT" \
  "$ADMQTT_MQTT_CLIENT_ID"

UNLOCK=/secure/path/ad-mqtt-enable-once
test ! -e "$UNLOCK" && test ! -L "$UNLOCK" || exit 1
test ! -e "$UNLOCK.consumed" && test ! -L "$UNLOCK.consumed" || exit 1
(set -o noclobber; : > "$UNLOCK") || exit 1
chmod 0600 "$UNLOCK"

RUN_ID=$(date -u +%Y%m%dT%H%M%SZ)
EVIDENCE_DIR="/secure/evidence/ad-mqtt-$RUN_ID"
mkdir -m 0700 "$EVIDENCE_DIR" || exit 1
install -m 0600 /dev/null "$EVIDENCE_DIR/ad-mqtt-canary.log"

export ADMQTT_COMMANDS_ENABLED=true
export ADMQTT_COMMANDS_UNLOCK_FILE="$UNLOCK"
export ADMQTT_LOG_LEVEL=INFO
export ADMQTT_LOG_SCREEN=true
export ADMQTT_LOG_FILE="$EVIDENCE_DIR/ad-mqtt-canary.log"

# Required for a release-qualifying canary. This file is not part of the evidence bundle.
CODE_FILE=/secure/config/ad-mqtt-panel-code
test -f "$CODE_FILE" && test ! -L "$CODE_FILE" || exit 1
stat -c '%U %G %a %n' "$CODE_FILE" "$(dirname "$CODE_FILE")"
unset ADMQTT_ALARM_CODE
export ADMQTT_ALARM_CODE_FILE="$CODE_FILE"
test -z "${ADMQTT_ALARM_CODE+x}" || exit 1

cd /etc/ad-mqtt-site
APPROVED_SHA='REPLACE_WITH_APPROVED_FULL_SHA'
/opt/ad-mqtt/venvs/"$APPROVED_SHA"/bin/python \
  /opt/ad-mqtt/releases/"$APPROVED_SHA"/run.py

# Run after the bridge exits.
unset ADMQTT_ALARM_CODE ADMQTT_ALARM_CODE_FILE
```

For an exploratory run only, replace the `CODE_FILE` block with the following, then unset the
variable immediately after shutdown:

```bash
unset ADMQTT_ALARM_CODE_FILE
read -rsp 'Panel code: ' ADMQTT_ALARM_CODE
printf '\n'
export ADMQTT_ALARM_CODE
```

Restrict the host and account because sufficiently privileged process inspection can read that
environment. Any subsequent G15 implementation creates a new RC and requires the qualifying
shadow and command canary.

Before publishing:

- the source unlock file is gone and `.consumed` exists;
- there is exactly one AD2/ser2sock client;
- the broker is stable and session-present/queue checks are clean;
- logs show confirmed DSC mode followed by a matching, non-programming KPM;
- the physical panel and KPM agree that it is disarmed and not already bypassed;
- `ready=true` before ordinary arm; and
- no unexplained warning, error, command, or reconnect has occurred.

### 8.2 Use one QoS 0, non-retained manual publisher

QoS 0 is defense in depth for this canary, not a protocol guarantee against broker queuing.
G9 and the broker-specific no-QoS0-queue proof must already pass. Never use `-r`; abort and
revoke publisher access on any disconnect. Provision a reviewed, restricted operator wrapper
like the administrative wrapper in section 7.2. It must inject only the approved broker,
identity, and TLS/authentication settings, pass the remaining arguments literally to
`mosquitto_pub`, and never log stdin or arguments. In publisher console 2, verify its path and
checksum, then read the code into a **non-exported** shell variable. These helpers keep the code
out of command history and out of every `mosquitto_pub` process environment:

```bash
OPERATOR_PUBLISH=/secure/bin/ad-mqtt-operator-publish
test -x "$OPERATOR_PUBLISH" && test ! -L "$OPERATOR_PUBLISH" || exit 1
sha256sum "$OPERATOR_PUBLISH"

read -rsp 'Panel code: ' PANEL_CODE
printf '\n'

publish_action() {
    action=$1
    printf '{"action":"%s","code":"%s"}' "$action" "$PANEL_CODE" |
        "$OPERATOR_PUBLISH" -q 0 -s \
            -t alarm/panel/set
}

publish_chime() {
    printf '%s' "$1" |
        "$OPERATOR_PUBLISH" -q 0 -s \
            -t alarm/panel/chime/set
}

publish_bypass() {
    printf '%s' "$1" |
        "$OPERATOR_PUBLISH" -q 0 -s \
            -t alarm/panel/bypass/set
}
```

One operator issues one action, then stops until all expected reports and physical/telemetry
state are recorded. Console 3 must independently tail the mode-0600 application log and observe
the approved MQTT telemetry topics read-only. Do not test duplicate delivery physically; the
contract suite covers it. Run `unset PANEL_CODE` in publisher console 2 at shutdown.

Production Home Assistant must not be repointed to the isolated broker for this command canary.
If a UI check is required, use a disposable sandbox HA instance with no command-topic write
permission. Otherwise the qualifying evidence is physical keypad + authoritative KPM + MQTT
state. Verify production HA discovery/entity continuity later in a separate commands-disabled
step with command-topic ACLs still denying writes.

### 8.3 Exercise one path at a time

Use a 30-second send-report deadline. Chime and disarm also require their terminal KPM within 30
seconds of the last expected send report. For arming, use the configured exit delay plus 30
seconds as the terminal-state deadline. A timeout is an abort, not permission to retry.

Use the approved two-person Away plan from section 7.4. Nobody may remain inside armed detection
coverage merely to stand at a keypad. Do not attempt Away with a lone operator.

The counts below are what the candidate currently expects, not established hardware facts. The
canary must confirm them, especially the three control bytes in S4/S5.

| Order | Path | Candidate-expected successful `!Sending` reports | Required terminal observation |
|---:|---|---:|---|
| 1 | Chime to opposite state | 2 for `*4` | Keypad and `chime_on` match target |
| 2 | Restore original chime | 2 | Original state restored |
| 3 | Stay | 3 for S4 × 3 | `armed_home` |
| 4 | Disarm | Access-code length | `disarmed` |
| 5 | Night/no-entry | Code length + 2 for `*9` | `armed_home`, `entry_delay_off=true` |
| 6 | Disarm | Access-code length | `disarmed` |
| 7 | Away | 3 for S5 × 3 | `armed_away`; safe exit behavior completes |
| 8 | Disarm | Access-code length | `disarmed` |
| 9 | One-zone bypass + Stay | 8; generally `6 + 2N` for N zones | `armed_home`, physical bypass true |
| 10 | Disarm and restore zone | Access-code length | Disarmed, fault restored, bypass cleared |

For bypass:

1. Start disarmed with no existing bypass.
2. Physically fault exactly the approved test zone.
3. Verify a fresh matching zone event and KPM.
4. Publish bypass `ON` and observe `alarm/panel/bypass/state=ON`.
5. In the same uninterrupted MQTT session, immediately issue `arm_stay`.
6. Verify the intent returns OFF after the attempt; this is not the physical bypass indicator.
7. Verify the separate physical bypass telemetry at `alarm/panel/bypass` and the keypad shows
   only the intended zone bypassed.
8. Disarm, restore the zone, and clear bypass locally if the panel does not do so.

For every action, record expected/observed response count and success, terminal KPM, physical
keypad state, MQTT state, and operator result. A cardinality mismatch—especially for three-byte
S4/S5—requires a new implementation/test/RC, not an adjusted retry.

### 8.4 Abort conditions

Abort immediately if:

- CONFIG is not `MODE=D`, changes, or a KPM marker disagrees;
- programming mode appears;
- MQTT, ser2sock, AD2, or the application disconnects/restarts;
- another writer or unknown publisher appears;
- a retained, duplicate, unsolicited, or malformed command is observed;
- a command is unexpectedly rejected/deferred;
- any `!Sending` result fails, is missing, extra, unmatched, stale, or has unexpected count;
- physical keypad, KPM, and MQTT state disagree;
- a terminal KPM misses its deadline;
- Stay/Away/Night/bypass behavior differs from the approved contract;
- an alarm, siren, fire/panic/trouble, unexpected monitoring/dispatch event, or any event outside
  the predeclared opening/closing test activity occurs;
- logs contain the alarm code; or
- SHA, dependency, configuration, or unlock evidence differs from the approved record.

## 9. Roll back or close the canary safely

Use the same controlled shutdown after a success; do not immediately leave commands enabled.

1. Announce that no more MQTT commands may be sent.
2. Revoke/disable the manual publisher first.
3. Do not send a remote retry or blind remote disarm.
4. Inspect and recover at the physical keypad. Disarm locally only if actually armed.
5. Stop the candidate and disable automatic restart.
6. Confirm the original unlock file remains absent; never recreate it as a retry.
7. Set `ADMQTT_COMMANDS_ENABLED=false` before any restart.
8. With the bridge stopped, clear the three retained command topics and purge its persistent
   broker session/queue.
9. Revoke temporary broker credentials.
10. Restore and verify the saved ser2sock firewall plus the broker's telemetry/read ACL baseline.
    Keep candidate credentials revoked and keep command-topic writes denied for every identity.
11. Reinspect/purge retained commands and persistent command queues on the legacy broker, then
    verify zero AD2/ser2sock clients after the candidate stops. Start the exact inventoried
    legacy client set in its documented order, then verify every expected identity and zero
    unknown clients. The baseline may include both legacy ad-mqtt and webapp; do not assume one
    service is the complete rollback.
12. Verify legacy physical and telemetry state is stable while command writes remain denied.
13. Restore HA read-side integrations and non-command automations. Keep every command-producing
    automation/publisher disabled, inspect for attempted commands, and purge the command queue
    again.
14. Restore the exact approved legacy command-write ACL only after the rollback owner confirms
    there is no pending command.
15. Re-enable the inventoried command producers one at a time as the final authority-transfer
    step, observing each for an unexpected publish.
16. End monitoring test mode only after the provider confirms no pending events.
17. Preserve restricted evidence and record pass/no-go plus every anomaly.

Any failure must become a regression test before another candidate is attempted.

## 10. Promote to production

Do not promote the attended canary process itself. Peer-review its evidence first.

Before production:

- complete G9-G15, including offline queued-command, alarm-state, observability, and production
  secret-interface tests;
- if those gates were completed after an exploratory canary, create a new RC and repeat the
  disabled shadow and full attended command canary;
- decide whether production will use the exact canaried host bundle or finish the target
  container/Compose path **before** the qualifying canary. A host-venv canary does not qualify a
  newly built container with a different Python/base image/network/restart environment;
- confirm the accepted RC SHA is already the reviewed protected-main SHA;
- create the final signed annotated tag, when possible, on that exact hardware-tested SHA;
- prove `git rev-list -n1 <tag>` equals the reviewed/deployed full SHA;
- promote/retag the exact canary-tested host bundle or image digest without rebuilding it. A
  post-tag rebuild is acceptable only if reproducible-build verification proves byte-identical
  digests/checksums; otherwise it is a new artifact requiring a new RC and qualifying canary;
- start commands-disabled and prove telemetry/availability before the scheduled ownership
  transfer; and
- document that every automatic restart fails closed until an operator inspects the panel and
  deliberately creates a fresh unlock.

For the target container path, implement:

- `python:3.13-slim` pinned by digest and a multi-stage build;
- prebuilt private wheels or a BuildKit secret that never enters a layer;
- non-root user, read-only-friendly paths, `.dockerignore`, and health check;
- OCI source/revision/version labels, SBOM, provenance, and private-GHCR publication on
  protected tags unless disclosure/licensing review approves public distribution;
- Compose pinned to an image digest, never `latest`; and
- a tightly scoped writable unlock mount with no blind restart loop.

Recreate and test webapp notifications as HA automations before retiring the webapp. Keep its
backup/restart path through the production soak, but keep the service stopped after command
ownership transfers. Leave ser2sock running.

## 11. Soak, monitoring, and final retirement

The current retained `alarm/available=online/offline` value has no heartbeat/timestamp, so it
cannot prove freshness. A missing `!Sending` response also leaves an in-memory latch without a
timeout event or metric. Complete G14 before production by adding either a heartbeat/timestamp or
an external broker-client/process liveness check, plus a payload-free in-flight age and
ACK-remaining signal. Crossing an age threshold must alert only; it must never clear, retry, or
unlock the operation. Test hung-process, broker loss, missing-ACK, and restart behavior.

Agree on a soak duration before rollout. With G14 in place, alert on:

- LWT offline plus the implemented heartbeat/external liveness threshold;
- MQTT/ser2sock reconnect churn;
- mode mismatches or programming vetoes;
- unmatched, excess, failed, or over-age missing `!Sending` results and ACKs remaining;
- `operator recovery required` and any ambiguous latch;
- duplicate/retained command attempts;
- state/zone/alarm/restore disagreement; and
- notification/automation failure.

An ambiguous latch is an inspect-and-disable event, never an automated unlock event.

After the soak:

1. rehearse rollback to the preserved artifact;
2. confirm fault/restore, arm/disarm, notification, and provider behavior;
3. retain sanitized release/canary/soak evidence and deployed SHA/tag/image digest;
4. archive the webapp database/configuration; and
5. only then rebuild the Pi as the minimal ser2sock bridge, keeping the SD/config backup and a
   tested spare.

The AD2Pi and serial path remain a physical single point of failure. Fast, rehearsed recovery is
the mitigation.

## 12. Broader v2 backlog

These items remain after the bounded command cutover. They should have their own milestones and
tests rather than being mixed into a hardware-tested safety commit.

1. Replace insteon-mqtt with asyncio plus paho-mqtt 2.x or aiomqtt; pin the supported MQTT major
   and remove unused transitive Jinja2/PyYAML.
2. Replace executable `devices.py` with a validated YAML zone configuration while preserving
   existing `ADMQTT_*` names during migration.
3. Add recorded, sanitized KPM/LRR/EXP/REL/RFX fixtures and complete Bridge event-to-topic
   assertions.
4. Correct the TLS, boolean parsing, faulted entity, unknown-zone alarm, alarm-restore, and
   tamper-class issues if they were not completed for production.
5. Finish the container, Compose, GHCR, CI, health, SBOM/provenance, and Proxmox deployment path.
6. Remove stale packaging/ceremony: `.bumpversion.cfg`, `notes.txt`, unused `restore_all`, the
   Config port typo, and `Client._fileno`.
7. Retire the webapp and reduce the Pi to a static ser2sock-only bridge only after production
   parity, notification migration, soak, and rollback rehearsal.

## Evidence/sign-off record

Store this outside the repository with restricted access:

```text
Candidate version / full SHA:
Builder AlarmDecoder source commit / wheel SHA-256:
Builder insteon-mqtt source commit / wheel SHA-256:
Artifact SHA-256 / image digest:
External config SHA-256:
Panel model / board / firmware / partition:
AD2 model / firmware / CONFIG mode:
Broker identity / fresh-session proof:
Exclusive-writer proof:
Monitoring test ticket and window:
24-hour shadow result and evidence location:
Per-action ACK/KPM evidence location:
Chime result:
Stay/disarm result:
Night/disarm result:
Away/disarm result:
Bypass/arm/disarm result:
Anomalies / regression links:
Rollback rehearsal result:
Operator sign-off / time:
Independent reviewer sign-off / time:
Production go/no-go decision:
Deployed tag / SHA / digest:
Soak start/end and outcome:
```
