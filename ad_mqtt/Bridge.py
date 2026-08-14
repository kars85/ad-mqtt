import datetime as DT
import json
import logging

from alarmdecoder.panels import ADEMCO, DSC

LOG = logging.getLogger(__name__)


class Bridge:
    def __init__(self, mqtt, decoder, code, zones, rf_devices,
                 commands_enabled=False, authorize_panel_write=None,
                 cancel_panel_write=None):
        if commands_enabled and not code:
            raise ValueError("An alarm code is required to enable commands")
        if commands_enabled and (
                authorize_panel_write is None
                or cancel_panel_write is None):
            raise ValueError(
                "Enabled commands require an authorizing panel transport"
            )

        self.mqtt = mqtt
        self.ad = decoder
        self.commands_enabled = commands_enabled
        self._authorize_panel_write = authorize_panel_write
        self._cancel_panel_write = cancel_panel_write
        self._panel_mode_confirmed = False
        self._confirmed_mode = None
        self._panel_state_confirmed = False
        self._confirmed_panel_status = None
        self._confirmed_chime_on = None
        self._confirmed_zone_bypassed = None
        self._confirmed_ready = None
        self._panel_programming_mode = None
        self._command_zone_states = {}
        self._pending_zone_events = {}
        self._action_write_token = None
        self._action_transmitted = False
        self._action_acknowledged = False
        self._action_ack_remaining = 0
        self._action_ack_failed = False
        self._dsc_action_in_flight = None
        self._dsc_action_used_bypass = False
        self._dsc_action_write_token = None
        self._dsc_action_transmitted = False
        self._dsc_action_acknowledged = False
        self._deferred_chime_target = None
        self._chime_toggle_target = None
        self._chime_write_token = None
        self._chime_transmitted = False
        self._chime_acknowledged = False
        self._chime_ack_remaining = 0
        self._chime_ack_failed = False
        self._pending_ack_tokens = []
        self.qos = 1
        self.retain = True
        self.panel_attr = {}
        self.panel_status = None
        self.is_bypass = False
        self.last_alarm_zone = ""

        self.code = code
        self.zones = zones
        self.rf_devices = rf_devices

        self.panel_state_topic = "alarm/panel/state"
        self.panel_set_topic = "alarm/panel/set"
        self.panel_msg_topic = "alarm/panel/message"
        self.panel_faulted_topic = "alarm/panel/faulted"
        self.panel_battery_topic = "alarm/panel/battery"
        self.panel_bypass_topic = "alarm/panel/bypass"

        self.chime_state_topic = "alarm/panel/chime/state"
        self.chime_set_topic = "alarm/panel/chime/set"

        self.bypass_state_topic = "alarm/panel/bypass/state"
        self.bypass_set_topic = "alarm/panel/bypass/set"

        self.sensor_state_topic = "alarm/sensor/{entity}/state"
        self.sensor_battery_topic = "alarm/sensor/{entity}/battery"

        self._connect()
        mqtt.signal_connected.connect(self.mqtt_connected)

    def reset_all_zones(self):
        # This is an explicit configured-zone reset, not a panel-derived
        # expander address, so it is safe before CONFIG identifies the mode.
        for zone_num in self.zones:
            mode_confirmed = (
                self._panel_mode_confirmed
                and self.ad.mode == self._confirmed_mode
            )
            self._update_zone_state(
                zone_num,
                False,
                command_trusted=mode_confirmed,
            )
            if not mode_confirmed:
                self._queue_zone_event(
                    zone_num,
                    False,
                    observed_mode=None,
                    publish_on_confirm=False,
                )

    def mqtt_connected(self, device, connected):
        if not connected:
            # Bypass is a volatile intent for the requester's next arm, not
            # durable panel state.  Once that MQTT session is gone, retaining
            # the intent could unexpectedly bypass a zone much later.
            if self.is_bypass:
                LOG.warning(
                    "Clearing bypass intent after MQTT disconnect"
                )
                self.is_bypass = False
            return

        # Treat every broker connection as a new intent epoch, even if the
        # MQTT wrapper did not emit an explicit disconnect notification.
        # Reconcile the retained state before accepting command traffic.
        if self.is_bypass:
            LOG.warning("Clearing bypass intent after MQTT reconnect")
        self._set_bypass_intent(False)
        if not self.commands_enabled:
            LOG.warning("Panel commands are disabled; MQTT commands ignored")
            return

        qos = 1
        self.mqtt.subscribe(self.panel_set_topic, qos, self.cb_panel_set)
        self.mqtt.subscribe(self.chime_set_topic, qos, self.cb_chime_set)
        self.mqtt.subscribe(self.bypass_set_topic, qos, self.cb_bypass_set)

    def cb_panel_set(self, client, user_data, message):
        LOG.info("Read panel set command")
        if not self.commands_enabled:
            LOG.warning("Ignoring panel action because commands are disabled")
            return
        if getattr(message, "retain", False):
            LOG.warning("Ignoring retained panel action")
            return
        if getattr(message, "dup", False):
            LOG.warning("Ignoring duplicate MQTT panel action")
            return

        try:
            msg = message.payload.decode("utf-8")
            data = json.loads(msg)
        except (UnicodeDecodeError, json.JSONDecodeError):
            LOG.exception("Error decoding panel set message")
            return

        if not isinstance(data, dict):
            LOG.error("Panel set message must be a JSON object")
            return

        input_code = data.get("code", None)
        if input_code != self.code:
            LOG.error("Invalid alarm code entered")
            return

        action = data.get("action", None)
        if action not in ("arm_away", "arm_stay", "arm_night", "disarm"):
            LOG.error("Invalid alarm action '%s'", action)
            return

        bypass_requested = self.is_bypass and action.startswith("arm_")
        keys = self._panel_action_keys(action)
        if keys is None:
            if bypass_requested:
                self._set_bypass_intent(False)
            return

        used_bypass = bypass_requested
        mode = self.ad.mode
        callback = self._on_action_transmitted
        token = self._authorize_panel_keys(keys, callback)
        if token is None:
            if used_bypass:
                self._set_bypass_intent(False)
            return

        if mode == DSC:
            # DSC commands include toggles and a code-only disarm that can
            # arm a panel if delivered twice.  Do not accept another panel
            # action until a matching KPM confirms this one's result.
            self._dsc_action_in_flight = action
            self._dsc_action_used_bypass = used_bypass
            self._dsc_action_write_token = token
            self._dsc_action_transmitted = False
            self._dsc_action_acknowledged = False

        self._action_write_token = token
        self._action_transmitted = False
        self._action_acknowledged = False
        self._action_ack_remaining = self._key_ack_count(keys)
        self._action_ack_failed = False

        LOG.info("Sending panel action '%s'", action)
        self._send_authorized_panel_keys(keys, token, callback)
        if used_bypass:
            # Bypass is an intent for one arm transaction, not a mode that
            # should silently carry into a later arm or reconnect.
            self._set_bypass_intent(False)

    def _panel_action_keys(self, action):
        mode = self._confirmed_panel_mode(action)
        if mode is None:
            return None

        if self._panel_programming_mode:
            LOG.warning("Ignoring panel action while panel is programming")
            return None

        if self._action_write_token is not None:
            pending_action = self._dsc_action_in_flight or "panel action"
            LOG.warning(
                "Ignoring panel action while %s is pending",
                pending_action,
            )
            return None

        if self._dsc_action_in_flight is not None:
            LOG.warning(
                "Ignoring panel action while DSC '%s' is pending",
                self._dsc_action_in_flight,
            )
            return None

        chime_blocks_disarm = (
            action == "disarm"
            and (not self._chime_transmitted
                 or not self._chime_acknowledged)
        )
        if (self._chime_toggle_target is not None
                and (action != "disarm" or chime_blocks_disarm)):
            LOG.warning(
                "Ignoring panel action while a chime toggle is pending"
            )
            return None

        if mode == DSC:
            if not self._panel_state_confirmed:
                LOG.warning(
                    "Ignoring DSC %s until panel state is confirmed",
                    action,
                )
                return None

            panel_can_be_disarmed = self._confirmed_panel_status in (
                "armed_away",
                "armed_home",
                "triggered",
            )
            if action == "disarm" and not panel_can_be_disarmed:
                # On DSC, the access code alone disarms an armed panel but can
                # arm a disarmed panel.  Never send it without known state.
                LOG.warning(
                    "Ignoring DSC disarm because the panel is not known "
                    "to be armed"
                )
                return None

            if (action.startswith("arm_")
                    and self._confirmed_panel_status != "disarmed"):
                # In particular, *9 toggles entry delay if DSC is already
                # armed.  All DSC arm actions therefore require a fresh,
                # positively disarmed state.
                LOG.warning(
                    "Ignoring DSC %s because the panel is not confirmed "
                    "disarmed",
                    action,
                )
                return None

            if (action.startswith("arm_")
                    and self._confirmed_zone_bypassed):
                LOG.warning(
                    "Ignoring DSC arm command while zones are already "
                    "bypassed"
                )
                return None

            if (action.startswith("arm_") and not self.is_bypass
                    and not self._confirmed_ready):
                LOG.warning(
                    "Ignoring DSC %s because the panel is not ready",
                    action,
                )
                return None

            action_keys = {
                "arm_away": self.ad.KEY_S5,
                "arm_stay": self.ad.KEY_S4,
                # DSC no-entry arming is the equivalent of Vista instant.
                "arm_night": f"*9{self.code}",
                "disarm": self.code,
            }
        else:
            action_keys = {
                "arm_away": f"{self.code}2",
                "arm_stay": f"{self.code}3",
                "arm_night": f"{self.code}7",
                "disarm": f"{self.code}1",
            }

        keys = ""
        if self.is_bypass and action.startswith("arm_"):
            if mode == DSC:
                keys = self._dsc_bypass_keys()
                if keys is None:
                    return None
            else:
                keys = f"{self.code}6#"

        return keys + action_keys[action]

    def _authorize_panel_keys(self, keys, callback):
        if self._authorize_panel_write is None:
            LOG.error("Panel transport cannot authorize physical writes")
            return None

        token = self._authorize_panel_write(keys, callback)
        if token is None:
            LOG.error("Panel transport rejected an authorized write")
        return token

    def _send_authorized_panel_keys(self, keys, token, callback):
        self.ad.send(keys)

    @staticmethod
    def _key_ack_count(keys):
        if not isinstance(keys, bytes):
            keys = keys.encode("utf-8")
        return len(keys)

    def _on_action_transmitted(self, token):
        if token != self._action_write_token:
            LOG.error("Ignoring stale panel-action transmission callback")
            return

        self._pending_ack_tokens.extend(
            (token, "action")
            for _index in range(self._action_ack_remaining)
        )
        self._action_transmitted = True
        if token == self._dsc_action_write_token:
            self._dsc_action_transmitted = True

    def _on_chime_transmitted(self, token):
        if token != self._chime_write_token:
            LOG.error("Ignoring stale chime transmission callback")
            return

        self._pending_ack_tokens.extend(
            (token, "chime")
            for _index in range(self._chime_ack_remaining)
        )
        self._chime_transmitted = True

    def _confirmed_panel_mode(self, command):
        if not self.commands_enabled:
            LOG.error(
                "Ignoring %s command because commands are disabled",
                command,
            )
            return None

        if not self._panel_mode_confirmed:
            LOG.error(
                "Ignoring %s command until AlarmDecoder confirms panel mode",
                command,
            )
            return None

        if (self.ad.mode not in (ADEMCO, DSC)
                or self.ad.mode != self._confirmed_mode):
            LOG.error(
                "Ignoring %s command for unconfirmed panel mode %r",
                command,
                self.ad.mode,
            )
            return None

        return self.ad.mode

    def _dsc_bypass_keys(self):
        if self._confirmed_zone_bypassed:
            # DSC zone selection toggles bypass state.  The bridge only has a
            # panel-wide bypass bit, so it cannot safely distinguish zones
            # that need bypassing from zones it would accidentally unbypass.
            LOG.error(
                "Ignoring DSC arm command: panel already has bypassed zones"
            )
            return None

        faulted_zones = set()
        for zone_num, faulted in self._command_zone_states.items():
            if faulted is not True:
                continue

            configured_zone = self.zones[zone_num].zone
            if (not isinstance(configured_zone, int)
                    or isinstance(configured_zone, bool)
                    or not 1 <= configured_zone <= 64):
                LOG.error(
                    "Ignoring DSC arm command: invalid faulted zone %r",
                    configured_zone,
                )
                return None

            faulted_zones.add(configured_zone)

        if not faulted_zones:
            LOG.error(
                "Ignoring DSC arm command: bypass requested without "
                "known faulted zones"
            )
            return None

        zones = "".join(f"{zone:02d}" for zone in sorted(faulted_zones))
        return f"*1{zones}#"

    def cb_chime_set(self, client, user_data, message):
        if getattr(message, "retain", False):
            LOG.warning("Ignoring retained chime command")
            return

        try:
            msg = message.payload.decode("utf-8").strip()
        except UnicodeDecodeError:
            LOG.exception("Error decoding chime command")
            return
        LOG.info("Read chime set '%s'", msg)

        normalized = msg.lower()
        if normalized not in ("on", "off"):
            LOG.warning("Ignoring invalid chime command")
            return

        chime_on = normalized == "on"
        self.set_chime(chime_on)

    def set_chime(self, chime_on):
        if not self.commands_enabled:
            LOG.warning("Ignoring chime command because commands are disabled")
            return
        if self._panel_programming_mode:
            LOG.warning("Ignoring chime command while panel is programming")
            self._deferred_chime_target = None
            return

        mode = self._confirmed_panel_mode("chime")
        if mode is None:
            self._deferred_chime_target = chime_on
            return

        if not self._panel_state_confirmed:
            LOG.info("Deferring chime command until panel state is confirmed")
            self._deferred_chime_target = chime_on
            return

        if (self._action_write_token is not None
                or self._dsc_action_in_flight is not None):
            LOG.info("Deferring chime command while a panel action is pending")
            self._deferred_chime_target = chime_on
            return

        if self._chime_toggle_target is not None:
            # Remember only a change of intent.  A matching request is
            # already represented by the in-flight target.
            self._deferred_chime_target = (
                None
                if chime_on == self._chime_toggle_target
                else chime_on
            )
            LOG.info("Deferring chime command while a toggle is pending")
            return

        # Chime is a physical toggle, not a set command.  Compare the desired
        # state with an authoritative KPM so startup/retry traffic cannot
        # toggle it in the wrong direction.
        if chime_on == self._confirmed_chime_on:
            self._deferred_chime_target = None
            return

        keys = "*4" if mode == DSC else self.code + "9"
        token = self._authorize_panel_keys(
            keys,
            self._on_chime_transmitted,
        )
        if token is None:
            return

        LOG.info("Toggling chime status")
        self._deferred_chime_target = None
        self._chime_toggle_target = chime_on
        self._chime_write_token = token
        self._chime_transmitted = False
        self._chime_acknowledged = False
        self._chime_ack_remaining = self._key_ack_count(keys)
        self._chime_ack_failed = False
        self._send_authorized_panel_keys(
            keys,
            token,
            self._on_chime_transmitted,
        )

    def cb_bypass_set(self, client, user_data, message):
        try:
            msg = message.payload.decode("utf-8").strip()
        except UnicodeDecodeError:
            LOG.exception("Error decoding bypass command")
            return
        normalized = msg.lower()
        if normalized not in ("on", "off"):
            LOG.warning("Ignoring invalid bypass command")
            return

        bypass_on = normalized == "on"

        if not self.commands_enabled:
            LOG.warning(
                "Ignoring bypass command because commands are disabled"
            )
            self._set_bypass_intent(False)
            return
        if getattr(message, "retain", False):
            LOG.warning("Ignoring retained bypass command")
            self._set_bypass_intent(False)
            return
        if getattr(message, "dup", False) and bypass_on:
            LOG.warning("Ignoring duplicate MQTT bypass ON command")
            return
        if self._panel_programming_mode:
            LOG.warning("Ignoring bypass command while panel is programming")
            self._set_bypass_intent(False)
            return

        LOG.info("Read bypass set '%s'", msg)
        self._set_bypass_intent(bypass_on)

    def _set_bypass_intent(self, bypass_on):
        self.is_bypass = bypass_on
        payload = {"status" : "ON" if self.is_bypass else "OFF"}
        self.publish(self.bypass_state_topic, {}, payload)

    def publish(self, topic, topic_args, payload_args, zone_num=None,
                zone=None):
        time = DT.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        payload_args["time"] = time

        if zone is not None or zone_num is not None:
            if not zone:
                zone = self.zones.get(zone_num)
                if not zone:
                    LOG.error("Skipping unknown zone %s", zone_num)
                    return

            if zone:
                topic_args["entity"] = zone.entity
                payload_args["zone_num"] = zone.zone

        try:
            topic_str = topic.format(**topic_args)
        except:
            LOG.exception("Error in MQTT topic formatting.\n"
                          "Topic: '%s'\nArgs: %s", topic, topic_args)
            return

        payload = json.dumps(payload_args)
        LOG.info("Publish '%s' = %s", topic_str, payload)
        self.mqtt.publish(topic_str, payload, qos=self.qos, retain=self.retain)

    def on_arm(self, dev, stay):
        LOG.info("on_arm %s", stay)
        self._update_panel_status("armed_home" if stay else "armed_away")

    def on_disarm(self, dev):
        LOG.info("on_disarm")
        self._update_panel_status("disarmed")

    def on_power_changed(self, dev, status):
        # message.ac_power
        pass

    def on_ready_changed(self, dev, status):
        pass

    def on_alarm(self, dev, zone):
        LOG.info("on_alarm zone: %s", zone)

        # Update the faulted zone first.
        info = self.zones.get(zone)
        if not info:
            LOG.error("Skipping unknown zone %s", zone)
        else:
            self.last_alarm_zone = info.entity

        self._update_panel_status("triggered", zone=zone)

    def on_alarm_restored(self, dev, zone, user=None):
        LOG.info("on_alarm_restored zone: %s", zone)
        self._update_panel_status("disarmed", zone=zone)
        self.last_alarm_zone = "None"

    def on_fire(self, dev, status):
        # message.fire_alarm
        pass

    def on_bypass(self, dev, status, zone=None):
        # status == bool
        LOG.info("on_bypass %s zone=%s", status, zone)

        payload = {"status" : "ON" if status else "OFF"}
        self.publish(self.panel_bypass_topic, {}, payload, zone_num=zone)

    def on_boot(self, dev):
        pass

    def on_config_received(self, dev):
        self._cancel_untransmitted_panel_writes()
        supported = dev.mode in (ADEMCO, DSC)
        first_confirmation = not self._panel_mode_confirmed
        mode_changed = (
            self._panel_mode_confirmed and self._confirmed_mode != dev.mode
        )

        if first_confirmation or mode_changed or not supported:
            if mode_changed:
                LOG.warning(
                    "Panel mode changed; invalidating command state"
                )
            # CONFIG establishes the panel-mode epoch.  State or EXP events
            # received before the first response may have been interpreted
            # using AlarmDecoder's default ADEMCO mode, so none of that state
            # may authorize commands.  Preserve only an unsent desired-state
            # chime request; never retry a toggle that was already sent.
            self._reset_command_state(
                preserve_deferred_chime=True,
                preserve_in_flight=True,
                preserve_programming_mode=True,
            )

        if (mode_changed or not supported) and self.is_bypass:
            self._set_bypass_intent(False)

        self._panel_mode_confirmed = supported
        self._confirmed_mode = dev.mode if supported else None
        if self._panel_mode_confirmed:
            panel_name = "DSC" if dev.mode == DSC else "ADEMCO"
            LOG.info("Confirmed %s panel mode", panel_name)
            self._apply_pending_zone_events(dev.mode)
        else:
            self._pending_zone_events.clear()
            LOG.error(
                "AlarmDecoder reported unsupported panel mode %r",
                dev.mode,
            )

    def on_zone_fault(self, dev, zone):
        LOG.info("on_zone_fault %s", zone)

        if (not self._panel_mode_confirmed
                or self.ad.mode != self._confirmed_mode):
            # Expander addresses map to different zones on ADEMCO and DSC.
            # AlarmDecoder defaults to ADEMCO until CONFIG arrives, so a
            # pre-CONFIG event is not safe to cache or publish as retained.
            LOG.warning("Deferring zone fault until panel mode confirmation")
            self._queue_zone_event(zone, True, observed_mode=self.ad.mode)
            return

        self._update_zone_state(zone, True, command_trusted=True)

    def _update_zone_state(self, zone, faulted, command_trusted=False,
                           publish=True):
        info = self.zones.get(zone)
        if info:
            LOG.debug("Setting zone %s = %s", zone, faulted)
            info.faulted = faulted
            if command_trusted:
                self._command_zone_states[zone] = faulted

        if publish:
            payload = {"status" : "ON" if faulted else "OFF"}
            self.publish(self.sensor_state_topic, {}, payload, zone_num=zone,
                         zone=info)

    def _queue_zone_event(self, zone, faulted, observed_mode,
                          publish_on_confirm=True):
        self._pending_zone_events[zone] = (
            faulted,
            observed_mode,
            publish_on_confirm,
        )

    def _apply_pending_zone_events(self, confirmed_mode):
        pending = self._pending_zone_events
        self._pending_zone_events = {}
        for zone, (faulted, observed_mode, publish_event) in pending.items():
            if observed_mode not in (None, confirmed_mode):
                LOG.warning(
                    "Discarding zone %s state observed under panel mode %r",
                    zone,
                    observed_mode,
                )
                continue
            self._update_zone_state(
                zone,
                faulted,
                command_trusted=True,
                publish=publish_event,
            )

    def on_zone_restore(self, dev, zone):
        LOG.info("on_zone_restored %s", zone)
        if (not self._panel_mode_confirmed
                or self.ad.mode != self._confirmed_mode):
            LOG.warning("Deferring zone restore until panel mode confirmation")
            self._queue_zone_event(zone, False, observed_mode=self.ad.mode)
            return

        self._update_zone_state(zone, False, command_trusted=True)

    def on_low_battery(self, dev, status):
        # status = bool
        payload = {"status" : 10 if status else 100}
        self.publish(self.panel_battery_topic, {}, payload)

    def on_panic(self, dev, status):
        # status == bool
        pass

    def on_relay_changed(self, dev, message):
        pass

    def on_chime_changed(self, dev, status):
        # status = bool
        payload = {"status" : "ON" if status else "OFF"}
        self.publish(self.chime_state_topic, {}, payload)

    def on_message(self, dev, message):
        LOG.debug("on_message %s", repr(message))

        msg = message
        command_state_accepted = self._record_command_state(msg)

        # first call: update sensor states
        if not self.panel_attr:
            self.on_chime_changed(dev, msg.chime_on)
            self.on_bypass(dev, msg.zone_bypassed)
            self.on_low_battery(dev, msg.battery_low)

        panel_attr = {
            "ac_power_on": msg.ac_power,
            "alarm_event_occurred": msg.alarm_event_occurred,
            "backlight_on": msg.backlight_on,
            "battery_low": msg.battery_low,
            "check_zone": msg.check_zone,
            "chime_on": msg.chime_on,
            "entry_delay_off": msg.entry_delay_off,
            "programming_mode": msg.programming_mode,
            "ready": msg.ready,
            "zone_bypassed": msg.zone_bypassed,
            }

        payload = {"status" : msg.text.strip()}
        self.publish(self.panel_msg_topic, {}, payload)

        # Update the panel state if this is the first call we've seen or if
        # the attributes have changed.
        if self.panel_status is None or self.panel_attr != panel_attr:
            status = self.panel_status
            if self.panel_status is None:
                status = "disarmed"
                if msg.alarm_sounding:
                    status = "triggered"
                elif msg.armed_away:
                    status = "armed_away"
                elif msg.armed_home:
                    status = "armed_home"

            self.panel_attr = panel_attr
            self._update_panel_status(status)

        # Resolve a desired-state request only from the first authoritative
        # post-CONFIG KPM.  Read-side telemetry above still flows when a KPM
        # cannot be trusted for commands.
        if command_state_accepted:
            pending_chime = self._deferred_chime_target
            if (pending_chime is not None
                    and self._chime_toggle_target is None):
                self.set_chime(pending_chime)

    def _record_command_state(self, message):
        # A KPM can be selected for reading before a command already queued
        # in Client is writable.  Cancel a wholly unsent command so this KPM
        # cannot make its eventual delivery unsafe.  A partial/ambiguous write
        # remains interlocked for operator recovery.
        self._cancel_untransmitted_panel_writes()

        # Programming is a safety veto even when CONFIG is not yet available
        # or the KPM panel marker disagrees.  Only a later authoritative,
        # matching non-programming KPM may clear this sticky state.
        if message.programming_mode:
            LOG.warning(
                "Panel is in programming mode; command state remains "
                "unconfirmed"
            )
            self._panel_programming_mode = True
            self._deferred_chime_target = None
            if self.is_bypass:
                self._set_bypass_intent(False)
            self._invalidate_confirmed_panel_state()
            return False

        if (not self._panel_mode_confirmed
                or self.ad.mode != self._confirmed_mode):
            LOG.info(
                "Panel telemetry is not command-authoritative before CONFIG"
            )
            self._invalidate_confirmed_panel_state()
            return False

        if message.panel_type != self._confirmed_mode:
            # Some AD2/keypad streams omit or disagree on this marker.  They
            # remain useful telemetry, but they must not authorize a command
            # family that could have opposite semantics on another panel.
            LOG.warning(
                "Panel message mode %r does not match CONFIG mode %r; "
                "command state remains unconfirmed",
                message.panel_type,
                self._confirmed_mode,
            )
            self._invalidate_confirmed_panel_state()
            return False

        self._panel_programming_mode = False

        if message.alarm_sounding:
            status = "triggered"
        elif message.armed_away:
            status = "armed_away"
        elif message.armed_home:
            status = "armed_home"
        else:
            status = "disarmed"

        self._panel_state_confirmed = True
        self._confirmed_panel_status = status
        self._confirmed_chime_on = message.chime_on
        self._confirmed_zone_bypassed = message.zone_bypassed
        self._confirmed_ready = message.ready

        if (self._chime_transmitted
                and self._chime_acknowledged
                and self._chime_toggle_target == message.chime_on):
            self._clear_chime_toggle()

        if (self._dsc_action_transmitted
                and self._dsc_action_acknowledged
                and self._dsc_action_completed(message, status)):
            self._clear_dsc_action()

        return True

    def _dsc_action_completed(self, message, status):
        action = self._dsc_action_in_flight
        if action is None or self._confirmed_mode != DSC:
            return False

        if action.startswith("arm_"):
            if status == "triggered":
                LOG.error(
                    "DSC %s reached triggered state; operator verification "
                    "required",
                    action,
                )
                return True
            if status not in ("armed_away", "armed_home"):
                return False

            expected_status = {
                "arm_away": "armed_away",
                "arm_stay": "armed_home",
                "arm_night": "armed_home",
            }[action]
            result_matches = status == expected_status
            if action == "arm_night":
                result_matches = result_matches and message.entry_delay_off
            if self._dsc_action_used_bypass:
                result_matches = result_matches and message.zone_bypassed

            if not result_matches:
                # The panel is definitively armed, so a duplicate arm remains
                # unsafe but disarm must not be locked out.  End the ambiguity
                # and report that the requested arm semantics were not seen.
                LOG.error(
                    "DSC %s reached unexpected armed state; operator "
                    "verification required",
                    action,
                )
            return True

        return action == "disarm" and status == "disarmed"

    def _invalidate_confirmed_panel_state(self):
        self._panel_state_confirmed = False
        self._confirmed_panel_status = None
        self._confirmed_chime_on = None
        self._confirmed_zone_bypassed = None
        self._confirmed_ready = None

    def _cancel_untransmitted_panel_writes(self):
        if (self._action_write_token is not None
                and not self._action_transmitted
                and self._cancel_panel_write is not None
                and self._cancel_panel_write(
                    self._action_write_token
                )):
            LOG.warning("Canceled panel action before transport transmission")
            if self._action_write_token == self._dsc_action_write_token:
                self._clear_dsc_action()
            else:
                self._clear_action_write()

        if (self._chime_write_token is not None
                and not self._chime_transmitted
                and self._cancel_panel_write is not None
                and self._cancel_panel_write(self._chime_write_token)):
            LOG.warning("Canceled chime toggle before transport transmission")
            self._clear_chime_toggle()

    def _clear_dsc_action(self):
        dsc_token = self._dsc_action_write_token
        self._dsc_action_in_flight = None
        self._dsc_action_used_bypass = False
        self._dsc_action_write_token = None
        self._dsc_action_transmitted = False
        self._dsc_action_acknowledged = False
        if self._action_write_token == dsc_token:
            self._clear_action_write()

    def _clear_action_write(self):
        self._action_write_token = None
        self._action_transmitted = False
        self._action_acknowledged = False
        self._action_ack_remaining = 0
        self._action_ack_failed = False

    def _clear_chime_toggle(self):
        self._chime_toggle_target = None
        self._chime_write_token = None
        self._chime_transmitted = False
        self._chime_acknowledged = False
        self._chime_ack_remaining = 0
        self._chime_ack_failed = False

    def on_sending_received(self, dev, status, message):
        if not self._pending_ack_tokens:
            LOG.warning("Ignoring unmatched AlarmDecoder sending response")
            return

        token, write_type = self._pending_ack_tokens.pop(0)
        if write_type == "action" and token == self._action_write_token:
            if self._action_ack_remaining <= 0:
                LOG.error("Ignoring excess panel-action sending response")
                return
            self._action_ack_remaining -= 1
            if not status:
                if not self._action_ack_failed:
                    LOG.error(
                        "AlarmDecoder rejected part of the pending panel "
                        "action; operator recovery required"
                    )
                self._action_ack_failed = True

            if self._action_ack_remaining:
                return
            if self._action_ack_failed:
                return

            self._action_acknowledged = True
            if token == self._dsc_action_write_token:
                self._dsc_action_acknowledged = True
            else:
                self._clear_action_write()
        elif write_type == "chime" and token == self._chime_write_token:
            if self._chime_ack_remaining <= 0:
                LOG.error("Ignoring excess chime sending response")
                return
            self._chime_ack_remaining -= 1
            if not status:
                if not self._chime_ack_failed:
                    LOG.error(
                        "AlarmDecoder rejected part of the pending chime "
                        "toggle; operator recovery required"
                    )
                self._chime_ack_failed = True

            if self._chime_ack_remaining:
                return
            if self._chime_ack_failed:
                return

            self._chime_acknowledged = True
        else:
            LOG.warning("Ignoring stale AlarmDecoder sending response")

    def on_expander_message(self, dev, message):
        LOG.info("on_expander_message %s", repr(message))
        if message.type != message.ZONE:
            return

        zone = self._expander_to_zone(
            message.address,
            message.channel,
            self.ad.mode,
        )
        if zone < 0:
            return

        faulted = message.value != 0
        mode_confirmed = (
            self._panel_mode_confirmed
            and self.ad.mode == self._confirmed_mode
        )
        if not mode_confirmed:
            self._queue_zone_event(
                zone,
                faulted,
                observed_mode=self.ad.mode,
            )
            return

        info = self.zones.get(zone)
        if (self._command_zone_states.get(zone) == faulted
                and (info is None or info.faulted == faulted)):
            return
        self._update_zone_state(zone, faulted, command_trusted=True)

    @staticmethod
    def _expander_to_zone(address, channel, panel_mode):
        if panel_mode == ADEMCO:
            index = address - 7
            return address + channel + (index * 7) + 1
        if panel_mode == DSC:
            return (address * 8) + channel
        return -1

    def on_lrr_message(self, dev, message):
        LOG.info("on_lrr_message %s", repr(message))

    def on_rfx_message(self, dev, message):
        msg = message
        serial = str(msg.serial_number)
        info = self.rf_devices.get(serial)
        if info is None:
            LOG.debug("Ignoring unknown RF serial no %s", msg.serial_number)
            return

        LOG.info("on_rfx_message serial=%s value=%s battery=%s supervision=%s "
                 "loop=%s", msg.serial_number, msg.value, msg.battery,
                 msg.supervision, msg.loop)

        battery_payload = None
        if msg.value is not None:
            is_low_battery = bool(msg.value & 0x02)
            battery_payload = {"status" : 10 if is_low_battery else 100}

        for i, loop in enumerate(info.loops):
            if not loop:
                continue

            if battery_payload:
                self.publish(self.sensor_battery_topic, {}, battery_payload,
                             zone_num=loop.zone)

            # True = faulted, False = cleared
            if msg.loop[i] == loop.faulted:
                # The source state may survive a transport reconnect while
                # command trust does not.  Refresh trust from every RF report
                # without republishing an unchanged sensor value.
                self._update_rf_zone_state(
                    loop.zone,
                    msg.loop[i],
                    publish=False,
                )
                continue

            if msg.loop[i]:
                # RF zone numbers come from explicit configuration, not the
                # panel-mode-dependent expander address mapping.
                self._update_rf_zone_state(loop.zone, True)
            else:
                self._update_rf_zone_state(loop.zone, False)

            LOG.debug("Setting zone %s loop %s = %s", loop.zone, i,
                      msg.loop[i])
            loop.faulted = msg.loop[i]

    def _update_rf_zone_state(self, zone, faulted, publish=True):
        mode_confirmed = (
            self._panel_mode_confirmed
            and self.ad.mode == self._confirmed_mode
        )
        self._update_zone_state(
            zone,
            faulted,
            command_trusted=mode_confirmed,
            publish=publish,
        )
        if not mode_confirmed:
            self._queue_zone_event(
                zone,
                faulted,
                observed_mode=None,
                publish_on_confirm=False,
            )

    def on_open(self, dev):
        LOG.info("on_open")
        self._cancel_untransmitted_panel_writes()
        self._reset_panel_confirmation()
        if self.is_bypass:
            self._set_bypass_intent(False)

    def on_close(self, dev):
        LOG.info("on_close")
        self._cancel_untransmitted_panel_writes()
        self._reset_panel_confirmation()
        if self.is_bypass:
            self._set_bypass_intent(False)

    def _reset_panel_confirmation(self):
        self._panel_mode_confirmed = False
        self._confirmed_mode = None
        self._pending_zone_events = {}
        self._reset_command_state(
            preserve_deferred_chime=True,
            preserve_in_flight=True,
        )

    def _reset_command_state(self, preserve_deferred_chime=False,
                             preserve_in_flight=False,
                             preserve_programming_mode=False):
        deferred_chime = (
            self._deferred_chime_target
            if preserve_deferred_chime
            else None
        )
        pending_action_write_token = (
            self._action_write_token
            if preserve_in_flight
            else None
        )
        pending_action_write_transmitted = (
            self._action_transmitted
            if preserve_in_flight
            else False
        )
        pending_action_write_acknowledged = (
            self._action_acknowledged
            if preserve_in_flight
            else False
        )
        pending_action_ack_remaining = (
            self._action_ack_remaining
            if preserve_in_flight
            else 0
        )
        pending_action_ack_failed = (
            self._action_ack_failed
            if preserve_in_flight
            else False
        )
        pending_action = (
            self._dsc_action_in_flight
            if preserve_in_flight
            else None
        )
        pending_action_used_bypass = (
            self._dsc_action_used_bypass
            if preserve_in_flight
            else False
        )
        pending_chime = (
            self._chime_toggle_target
            if preserve_in_flight
            else None
        )
        pending_action_token = (
            self._dsc_action_write_token
            if preserve_in_flight
            else None
        )
        pending_action_transmitted = (
            self._dsc_action_transmitted
            if preserve_in_flight
            else False
        )
        pending_action_acknowledged = (
            self._dsc_action_acknowledged
            if preserve_in_flight
            else False
        )
        pending_chime_token = (
            self._chime_write_token
            if preserve_in_flight
            else None
        )
        pending_chime_transmitted = (
            self._chime_transmitted
            if preserve_in_flight
            else False
        )
        pending_chime_acknowledged = (
            self._chime_acknowledged
            if preserve_in_flight
            else False
        )
        pending_chime_ack_remaining = (
            self._chime_ack_remaining
            if preserve_in_flight
            else 0
        )
        pending_chime_ack_failed = (
            self._chime_ack_failed
            if preserve_in_flight
            else False
        )
        programming_mode = (
            self._panel_programming_mode
            if preserve_programming_mode
            else None
        )
        self._invalidate_confirmed_panel_state()
        self._panel_programming_mode = None
        self._command_zone_states = {}
        self._action_write_token = pending_action_write_token
        self._action_transmitted = pending_action_write_transmitted
        self._action_acknowledged = pending_action_write_acknowledged
        self._action_ack_remaining = pending_action_ack_remaining
        self._action_ack_failed = pending_action_ack_failed
        self._dsc_action_in_flight = pending_action
        self._dsc_action_used_bypass = pending_action_used_bypass
        self._dsc_action_write_token = pending_action_token
        self._dsc_action_transmitted = pending_action_transmitted
        self._dsc_action_acknowledged = pending_action_acknowledged
        self._deferred_chime_target = deferred_chime
        self._chime_toggle_target = pending_chime
        self._chime_write_token = pending_chime_token
        self._chime_transmitted = pending_chime_transmitted
        self._chime_acknowledged = pending_chime_acknowledged
        self._chime_ack_remaining = pending_chime_ack_remaining
        self._chime_ack_failed = pending_chime_ack_failed
        self._panel_programming_mode = programming_mode

    def _connect(self):
        self.ad.on_arm += self.on_arm
        self.ad.on_disarm += self.on_disarm
        self.ad.on_power_changed += self.on_power_changed
        self.ad.on_ready_changed += self.on_ready_changed
        self.ad.on_alarm += self.on_alarm
        self.ad.on_alarm_restored += self.on_alarm_restored
        self.ad.on_fire += self.on_fire
        self.ad.on_bypass += self.on_bypass
        self.ad.on_boot += self.on_boot
        self.ad.on_config_received += self.on_config_received
        self.ad.on_zone_fault += self.on_zone_fault
        self.ad.on_zone_restore += self.on_zone_restore
        self.ad.on_low_battery += self.on_low_battery
        self.ad.on_panic += self.on_panic
        self.ad.on_relay_changed += self.on_relay_changed
        self.ad.on_chime_changed += self.on_chime_changed
        self.ad.on_message += self.on_message
        self.ad.on_expander_message += self.on_expander_message
        self.ad.on_lrr_message += self.on_lrr_message
        self.ad.on_rfx_message += self.on_rfx_message
        self.ad.on_open += self.on_open
        self.ad.on_close += self.on_close
        self.ad.on_sending_received += self.on_sending_received

    def _update_panel_status(self, status, zone=None):
        self.panel_status = status

        payload = {"status" : self.panel_status, "attr" : self.panel_attr,
                   "last_alarm_zone" : self.last_alarm_zone}
        self.publish(self.panel_state_topic, {}, payload, zone_num=zone)
