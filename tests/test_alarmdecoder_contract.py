import ast
import inspect
import json
import runpy
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from types import ModuleType
from unittest import TestCase

from alarmdecoder import AlarmDecoder
from alarmdecoder.event import event
from alarmdecoder.event.event import Event
from alarmdecoder.messages import Message, RFMessage
from alarmdecoder.panels import ADEMCO, DSC

REPO_ROOT = Path(__file__).resolve().parents[1]

# Importing ad_mqtt eagerly imports the legacy insteon-mqtt transport.  Load
# Bridge directly so the contract suite needs AlarmDecoder but no broker,
# ser2sock instance, or insteon-mqtt installation.
Bridge = runpy.run_path(str(REPO_ROOT / "ad_mqtt" / "Bridge.py"))["Bridge"]

MODE_CODES = {ADEMCO: "A", DSC: "D"}


def panel_message(
        mode,
        *,
        armed_away=False,
        armed_home=False,
        zone_bypassed=False,
        chime_on=False,
        alarm_sounding=False,
        entry_delay_off=False,
        programming_mode=False,
        ready=False,
        numeric_code="000",
        text=""):
    """Build a KPM whose panel marker and state bits are explicit."""
    bits = ["0"] * 17
    bit_values = {
        1: ready,
        2: armed_away,
        3: armed_home,
        5: programming_mode,
        7: zone_bypassed,
        9: chime_on,
        11: alarm_sounding,
        13: entry_delay_off,
    }
    for bit, enabled in bit_values.items():
        bits[bit - 1] = "1" if enabled else "0"

    bitfield = "".join(bits) + MODE_CODES[mode] + "--"
    alpha = text[:32].ljust(32)
    return (
        f'!KPM:[{bitfield}],{numeric_code},'
        f'[f707000600e5800c0c020000],"{alpha}"'
    ).encode()


PANEL_MESSAGE = panel_message(ADEMCO)


EVENT_CALLBACKS = {
    "on_arm": ("on_arm", "(dev, stay)"),
    "on_disarm": ("on_disarm", "(dev)"),
    "on_power_changed": ("on_power_changed", "(dev, status)"),
    "on_ready_changed": ("on_ready_changed", "(dev, status)"),
    "on_alarm": ("on_alarm", "(dev, zone)"),
    "on_alarm_restored": ("on_alarm_restored", "(dev, zone, user=None)"),
    "on_fire": ("on_fire", "(dev, status)"),
    "on_bypass": ("on_bypass", "(dev, status, zone=None)"),
    "on_boot": ("on_boot", "(dev)"),
    "on_config_received": ("on_config_received", "(dev)"),
    "on_zone_fault": ("on_zone_fault", "(dev, zone)"),
    "on_zone_restore": ("on_zone_restore", "(dev, zone)"),
    "on_low_battery": ("on_low_battery", "(dev, status)"),
    "on_panic": ("on_panic", "(dev, status)"),
    "on_relay_changed": ("on_relay_changed", "(dev, message)"),
    "on_chime_changed": ("on_chime_changed", "(dev, status)"),
    "on_message": ("on_message", "(dev, message)"),
    "on_expander_message": ("on_expander_message", "(dev, message)"),
    "on_lrr_message": ("on_lrr_message", "(dev, message)"),
    "on_rfx_message": ("on_rfx_message", "(dev, message)"),
    "on_open": ("on_open", "(dev)"),
    "on_close": ("on_close", "(dev)"),
    "on_sending_received": (
        "on_sending_received",
        "(dev, status, message)",
    ),
}


class FakeDevice:
    on_open = event.Event()
    on_close = event.Event()
    on_read = event.Event()
    on_write = event.Event()

    def __init__(self):
        self.writes = []
        self.blocked_writes = []
        self.queued_writes = []
        self.defer_authorized_writes = False
        self.uncancelable_writes = False
        self.pending_acknowledgements = 0
        self._authorized_write = None

    def authorize_write(self, data, on_sent=None):
        if not isinstance(data, bytes):
            data = data.encode()
        if self._authorized_write is not None:
            return None
        token = object()
        self._authorized_write = (token, data, on_sent)
        return token

    def cancel_write(self, token):
        if self.uncancelable_writes:
            if (self._authorized_write is not None
                    and self._authorized_write[0] == token):
                self._authorized_write = None
                return False
            for index, queued in enumerate(self.queued_writes):
                if queued[0] == token:
                    self.queued_writes.pop(index)
                    return False
            return False
        if (self._authorized_write is not None
                and self._authorized_write[0] == token):
            self._authorized_write = None
            return True
        for index, queued in enumerate(self.queued_writes):
            if queued[0] == token:
                self.queued_writes.pop(index)
                return True
        return False

    def write(self, data):
        if not isinstance(data, bytes):
            data = data.encode()
        if data in (b"C\r", b"V\r"):
            self.writes.append(data)
            self.on_write(data=data)
            return

        authorization = self._authorized_write
        if authorization is None or authorization[1] != data:
            self.blocked_writes.append(data)
            return

        token, _authorized_data, on_sent = authorization
        self._authorized_write = None
        if self.defer_authorized_writes:
            self.queued_writes.append((token, data, on_sent))
            return

        self.writes.append(data)
        self.on_write(data=data)
        if on_sent is not None:
            on_sent(token)
        self.pending_acknowledgements += len(data)

    def flush_writes(self):
        queued_writes = self.queued_writes
        self.queued_writes = []
        for token, data, on_sent in queued_writes:
            self.writes.append(data)
            self.on_write(data=data)
            if on_sent is not None:
                on_sent(token)
            self.pending_acknowledgements += len(data)

    def acknowledge_writes(self, status=True, count=None):
        if count is None:
            count = self.pending_acknowledgements
        else:
            count = min(count, self.pending_acknowledgements)
        self.pending_acknowledgements -= count
        response = b"!Sending.done" if status else b"!Sending.....done"
        for _index in range(count):
            self.on_read(data=response)


class FakeSignal:
    def __init__(self):
        self.callbacks = []

    def connect(self, callback):
        self.callbacks.append(callback)

    def emit(self, *args):
        for callback in list(self.callbacks):
            callback(*args)


class FakeMqtt:
    def __init__(self):
        self.signal_connected = FakeSignal()
        self.publications = []
        self.subscriptions = []

    def publish(self, topic, payload, **kwargs):
        self.publications.append(
            {
                "topic": topic,
                "payload": json.loads(payload),
                "kwargs": kwargs,
            }
        )

    def subscribe(self, *args):
        self.subscriptions.append(args)


class FakeZone:
    def __init__(self, zone, entity):
        self.zone = zone
        self.entity = entity
        self.faulted = None


class FakeRfDevice:
    def __init__(self, loop_zero):
        self.loops = [loop_zero, None, None, None]


class AlarmDecoderContractTest(TestCase):
    def setUp(self):
        self.reset_harness()

    def reset_harness(self):
        self.device = FakeDevice()
        self.decoder = AlarmDecoder(self.device)
        self.decoder.wire_events()
        self.mqtt = FakeMqtt()

    def make_bridge(self, zones=None, rf_devices=None, commands_enabled=True):
        return Bridge(
            self.mqtt,
            self.decoder,
            "0000",
            zones or {},
            rf_devices or {},
            commands_enabled=commands_enabled,
            authorize_panel_write=self.device.authorize_write,
            cancel_panel_write=self.device.cancel_write,
        )

    @staticmethod
    def load_client_type():
        fake_package = ModuleType("insteon_mqtt")
        fake_package.__path__ = []
        fake_network = ModuleType("insteon_mqtt.network")

        class FakeLink:
            def __init__(self):
                self.signal_closing = FakeSignal()
                self.signal_connected = FakeSignal()
                self.signal_needs_write = FakeSignal()

        fake_network.Link = FakeLink
        fake_package.network = fake_network
        module_names = ("insteon_mqtt", "insteon_mqtt.network")
        previous_modules = {
            name: sys.modules.get(name)
            for name in module_names
        }
        try:
            sys.modules["insteon_mqtt"] = fake_package
            sys.modules["insteon_mqtt.network"] = fake_network
            return runpy.run_path(
                str(REPO_ROOT / "ad_mqtt" / "Client.py")
            )["Client"]
        finally:
            for name, previous in previous_modules.items():
                if previous is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = previous

    def publications_for(self, topic):
        return [
            publication
            for publication in self.mqtt.publications
            if publication["topic"] == topic
        ]

    def confirm_panel_mode(self, mode):
        self.device.on_read(
            data=(
                "!CONFIG>MODE={}&CONFIGBITS=ff04&ADDRESS=18&LRR=N&COM=N&"
                "EXP=NNNNN&REL=NNNN&MASK=ffffffff&DEDUPLICATE=N".format(
                    MODE_CODES[mode]
                ).encode()
            )
        )
        self.assertEqual(self.decoder.mode, mode)

    def send_panel_state(self, mode=None, acknowledge=True, **state):
        mode = self.decoder.mode if mode is None else mode
        data = panel_message(mode, **state)
        parsed = Message(data.decode())
        self.assertEqual(parsed.panel_type, mode)
        if acknowledge:
            self.device.acknowledge_writes()
        self.device.on_read(data=data)
        return parsed

    @staticmethod
    def mqtt_message(payload, retain=False, dup=False):
        if isinstance(payload, str):
            payload = payload.encode()
        return SimpleNamespace(payload=payload, retain=retain, dup=dup)

    def send_panel_action(self, bridge, action, code="0000"):
        bridge.cb_panel_set(
            None,
            None,
            self.mqtt_message(json.dumps({"action": action, "code": code})),
        )

    def test_runtime_uses_public_alarmdecoder_wiring_api(self):
        tree = ast.parse((REPO_ROOT / "ad_mqtt" / "run.py").read_text())
        decoder_calls = [
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "decoder"
        ]

        self.assertIn("wire_events", decoder_calls)
        self.assertNotIn("_wire_events", decoder_calls)

    def test_commands_are_disabled_by_default_at_mqtt_and_transport(self):
        self.assertFalse(
            inspect.signature(Bridge).parameters[
                "commands_enabled"
            ].default
        )
        config_type = runpy.run_path(
            str(REPO_ROOT / "ad_mqtt" / "Config.py")
        )["Config"]
        config = config_type()
        self.assertFalse(config.alarm.commands_enabled)
        self.assertIsNone(config.alarm.command_unlock_file)
        with tempfile.TemporaryDirectory() as temp_dir:
            unlock_path = Path(temp_dir) / "enable-commands-once"
            unlock_path.touch()
            consumed_path = config.consume_command_unlock(str(unlock_path))
            self.assertFalse(unlock_path.exists())
            self.assertTrue(Path(consumed_path).exists())
            with self.assertRaises(RuntimeError):
                config.consume_command_unlock(str(unlock_path))
        self.assertIn(
            "ADMQTT_COMMANDS_ENABLED",
            (REPO_ROOT / "run.py").read_text(),
        )
        self.assertIn(
            "ADMQTT_COMMANDS_UNLOCK_FILE",
            (REPO_ROOT / "run.py").read_text(),
        )

        runtime_tree = ast.parse(
            (REPO_ROOT / "ad_mqtt" / "run.py").read_text()
        )
        for constructor in ("Client", "Bridge"):
            calls = [
                node
                for node in ast.walk(runtime_tree)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == constructor
            ]
            self.assertEqual(len(calls), 1)
            self.assertIn(
                "commands_enabled",
                {keyword.arg for keyword in calls[0].keywords},
            )
        bridge_call = next(
            call
            for call in ast.walk(runtime_tree)
            if isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name)
            and call.func.id == "Bridge"
        )
        bridge_keywords = {keyword.arg for keyword in bridge_call.keywords}
        self.assertIn("authorize_panel_write", bridge_keywords)
        self.assertIn("cancel_panel_write", bridge_keywords)

        bridge = self.make_bridge(commands_enabled=False)
        bridge.mqtt_connected(self.mqtt, True)
        self.assertListEqual(self.mqtt.subscriptions, [])
        bypass_states = self.publications_for(bridge.bypass_state_topic)
        self.assertEqual(bypass_states[-1]["payload"]["status"], "OFF")

        self.confirm_panel_mode(DSC)
        self.send_panel_state(DSC, ready=True)
        self.device.writes.clear()
        self.send_panel_action(bridge, "arm_away")
        bridge.cb_chime_set(None, None, self.mqtt_message("ON"))
        bridge.cb_bypass_set(None, None, self.mqtt_message("ON"))
        self.assertListEqual(self.device.writes, [])
        self.assertFalse(bridge.is_bypass)
        self.assertIsNone(bridge._deferred_chime_target)

        client_type = self.load_client_type()

        client = client_type(commands_enabled=False)
        self.assertFalse(
            inspect.signature(client_type).parameters[
                "commands_enabled"
            ].default
        )
        client.write("*")
        client.write("0000")
        self.assertEqual(client._write_buf, b"")
        client.write("C\r")
        client.write(b"V\r")
        self.assertEqual(client._write_buf, b"C\rV\r")
        write_interest = []
        client.signal_needs_write.connect(
            lambda _link, active: write_interest.append(active)
        )
        client.poll(0)
        self.assertListEqual(write_interest, [True])

        transmitted = []

        class FakeSocket:
            @staticmethod
            def send(data):
                return len(data)

        client = client_type(commands_enabled=True)
        client.socket = FakeSocket()
        client.write("*")
        self.assertEqual(client._write_buf, b"")
        token = client.authorize_write(
            "*4",
            on_sent=transmitted.append,
        )
        self.assertIsNotNone(token)
        client.write("*4")
        self.assertEqual(client._write_buf, b"*4")
        client.write_to_link(0)
        self.assertEqual(client._write_buf, b"")
        self.assertListEqual(transmitted, [token])

        token = client.authorize_write("0000")
        client.write("0000")
        self.assertTrue(client.cancel_write(token))
        self.assertEqual(client._write_buf, b"")

        class PartialSocket:
            @staticmethod
            def send(data):
                return 2

        client = client_type(commands_enabled=True)
        client.socket = PartialSocket()
        transmitted = []
        token = client.authorize_write(
            "*90000",
            on_sent=transmitted.append,
        )
        client.write("*90000")
        client.write_to_link(0)
        self.assertEqual(client._write_buf, b"0000")
        self.assertFalse(client.cancel_write(token))
        self.assertEqual(client._write_buf, b"")
        self.assertListEqual(client._write_segments, [])
        self.assertListEqual(transmitted, [])

        with self.assertRaises(ValueError):
            Bridge(
                self.mqtt,
                self.decoder,
                "",
                {},
                {},
                commands_enabled=True,
            )
        with self.assertRaises(ValueError):
            Bridge(
                self.mqtt,
                self.decoder,
                "0000",
                {},
                {},
                commands_enabled=True,
            )
        root_runner = (REPO_ROOT / "run.py").read_text()
        self.assertNotIn('ADMQTT_ALARM_CODE","1234', root_runner)
        self.assertIn(
            "ADMQTT_ALARM_CODE is required when panel commands are enabled",
            root_runner,
        )

    def test_exact_event_subscriptions_and_handler_signatures(self):
        bridge = self.make_bridge()

        declared_events = {
            name
            for name, value in vars(AlarmDecoder).items()
            if isinstance(value, Event)
        }
        subscribed_events = {
            name
            for name in declared_events
            if list(getattr(self.decoder, name))
        }
        self.assertSetEqual(subscribed_events, set(EVENT_CALLBACKS))
        self.assertEqual(len(subscribed_events), 23)

        for event_name, (callback_name, signature) in EVENT_CALLBACKS.items():
            callback = getattr(bridge, callback_name)
            self.assertListEqual(
                list(getattr(self.decoder, event_name)),
                [callback],
            )
            self.assertEqual(str(inspect.signature(callback)), signature)

    def test_client_serializes_vista_action_and_chime_until_all_key_acks(self):
        client_type = self.load_client_type()
        client = client_type(commands_enabled=True)
        decoder = AlarmDecoder(client)
        decoder.wire_events()
        mqtt = FakeMqtt()
        bridge = Bridge(
            mqtt,
            decoder,
            "0000",
            {},
            {},
            commands_enabled=True,
            authorize_panel_write=client.authorize_write,
            cancel_panel_write=client.cancel_write,
        )

        client.on_read(
            data=(
                b"!CONFIG>MODE=A&CONFIGBITS=ff04&ADDRESS=18&LRR=N&COM=N&"
                b"EXP=NNNNN&REL=NNNN&MASK=ffffffff&DEDUPLICATE=N"
            )
        )
        client.on_read(data=panel_message(ADEMCO, chime_on=False))
        self.send_panel_action(bridge, "disarm")
        bridge.cb_chime_set(None, None, self.mqtt_message("ON"))

        self.assertEqual(client._write_buf, b"00001")
        self.assertEqual(len(client._write_segments), 1)

        class FullWriteSocket:
            @staticmethod
            def send(data):
                return len(data)

        client.socket = FullWriteSocket()
        client.write_to_link(0)
        self.assertEqual(client._write_buf, b"")
        self.assertEqual(bridge._action_ack_remaining, 5)

        client.on_read(data=b"!Sending.done")
        self.assertIsNotNone(bridge._action_write_token)
        self.assertEqual(bridge._action_ack_remaining, 4)
        for _index in range(4):
            client.on_read(data=b"!Sending.done")
        self.assertIsNone(bridge._action_write_token)

        client.on_read(data=panel_message(ADEMCO, chime_on=False))
        self.assertEqual(client._write_buf, b"00009")
        self.assertEqual(len(client._write_segments), 1)

    def test_client_closes_on_eof_so_the_manager_can_reconnect(self):
        client_type = self.load_client_type()
        client = client_type(commands_enabled=False)
        closed = []
        client.signal_closing.connect(lambda link: closed.append(link))

        class EofSocket:
            was_closed = False

            @staticmethod
            def recv(_size):
                return b""

            @staticmethod
            def shutdown(_how):
                return None

            @classmethod
            def close(cls):
                cls.was_closed = True

        client.socket = EofSocket()
        self.assertEqual(client.read_from_link(), -1)
        self.assertListEqual(closed, [client])
        self.assertTrue(EofSocket.was_closed)
        self.assertIsNone(client.socket)

    def test_programming_kpm_discards_a_partial_action_suffix(self):
        client_type = self.load_client_type()
        client = client_type(commands_enabled=True)
        decoder = AlarmDecoder(client)
        decoder.wire_events()
        bridge = Bridge(
            FakeMqtt(),
            decoder,
            "0000",
            {},
            {},
            commands_enabled=True,
            authorize_panel_write=client.authorize_write,
            cancel_panel_write=client.cancel_write,
        )
        client.on_read(
            data=(
                b"!CONFIG>MODE=D&CONFIGBITS=ff04&ADDRESS=18&LRR=N&COM=N&"
                b"EXP=NNNNN&REL=NNNN&MASK=ffffffff&DEDUPLICATE=N"
            )
        )
        client.on_read(data=panel_message(DSC, ready=True))

        class PartialSocket:
            writes = []

            @classmethod
            def send(cls, data):
                count = 2 if not cls.writes else len(data)
                cls.writes.append(bytes(data[:count]))
                return count

        client.socket = PartialSocket()
        self.send_panel_action(bridge, "arm_night")
        client.write_to_link(0)
        self.assertListEqual(PartialSocket.writes, [b"*9"])
        self.assertEqual(client._write_buf, b"0000")

        client.on_read(data=panel_message(DSC, programming_mode=True))
        self.assertEqual(client._write_buf, b"")
        self.assertListEqual(client._write_segments, [])
        self.assertEqual(bridge._dsc_action_in_flight, "arm_night")
        self.assertFalse(bridge._dsc_action_transmitted)

        client.write_to_link(0)
        self.assertListEqual(PartialSocket.writes, [b"*9"])

    def test_pre_config_kpm_is_published_but_cannot_authorize_commands(self):
        bridge = self.make_bridge()

        self.send_panel_state(DSC, armed_away=True)
        self.assertEqual(len(self.publications_for(bridge.panel_msg_topic)), 1)
        self.assertFalse(bridge._panel_state_confirmed)
        self.send_panel_action(bridge, "disarm")
        self.assertListEqual(self.device.writes, [])

        self.confirm_panel_mode(DSC)
        self.send_panel_action(bridge, "disarm")
        self.assertListEqual(self.device.writes, [])

        self.send_panel_state(DSC, armed_away=True)
        self.device.writes.clear()
        self.send_panel_action(bridge, "disarm")
        self.assertListEqual(self.device.writes, [b"0000"])

    def test_mode_mismatched_kpm_stays_telemetry_only(self):
        bridge = self.make_bridge()
        self.confirm_panel_mode(DSC)

        self.send_panel_state(ADEMCO, armed_away=True)
        self.assertEqual(len(self.publications_for(bridge.panel_msg_topic)), 1)
        self.assertFalse(bridge._panel_state_confirmed)
        self.send_panel_action(bridge, "disarm")
        self.assertListEqual(self.device.writes, [])

        self.send_panel_state(DSC, armed_away=True)
        self.device.writes.clear()
        self.send_panel_action(bridge, "disarm")
        self.assertListEqual(self.device.writes, [b"0000"])

    def test_reconnect_requires_new_config_and_matching_panel_state(self):
        bridge = self.make_bridge()
        self.confirm_panel_mode(DSC)
        self.send_panel_state(DSC, armed_away=True)

        self.device.on_close()
        self.assertFalse(bridge._panel_mode_confirmed)
        self.assertFalse(bridge._panel_state_confirmed)
        self.device.on_open()
        self.assertListEqual(self.device.writes, [b"C\r", b"V\r"])

        self.confirm_panel_mode(DSC)
        self.device.writes.clear()
        self.send_panel_action(bridge, "disarm")
        self.assertListEqual(self.device.writes, [])

        self.send_panel_state(DSC, armed_away=True)
        self.device.writes.clear()
        self.send_panel_action(bridge, "disarm")
        self.assertListEqual(self.device.writes, [b"0000"])

    def test_reconnect_preserves_ambiguous_dsc_action_and_chime_toggle(self):
        bridge = self.make_bridge()
        self.confirm_panel_mode(DSC)
        self.send_panel_state(DSC, armed_away=True, chime_on=False)
        self.device.writes.clear()
        self.send_panel_action(bridge, "disarm")
        self.assertListEqual(self.device.writes, [b"0000"])

        self.device.on_close()
        self.device.on_open()
        self.confirm_panel_mode(DSC)
        self.send_panel_state(DSC, armed_away=True, chime_on=False)
        self.send_panel_action(bridge, "disarm")
        self.assertListEqual(
            self.device.writes,
            [b"0000", b"C\r", b"V\r"],
        )
        self.assertEqual(bridge._dsc_action_in_flight, "disarm")

        self.send_panel_state(DSC, chime_on=False)
        self.assertIsNone(bridge._dsc_action_in_flight)

        self.reset_harness()
        bridge = self.make_bridge()
        self.confirm_panel_mode(DSC)
        self.send_panel_state(DSC, chime_on=False)
        self.device.writes.clear()
        bridge.cb_chime_set(None, None, self.mqtt_message("ON"))

        self.device.on_close()
        self.device.on_open()
        self.confirm_panel_mode(DSC)
        self.send_panel_state(DSC, chime_on=False)
        bridge.cb_chime_set(None, None, self.mqtt_message("ON"))
        self.assertListEqual(
            self.device.writes,
            [b"*4", b"C\r", b"V\r"],
        )
        self.assertEqual(bridge._chime_toggle_target, True)
        self.send_panel_state(DSC, chime_on=True)
        self.assertIsNone(bridge._chime_toggle_target)

    def test_mode_change_does_not_clear_ambiguous_dsc_action(self):
        bridge = self.make_bridge()
        self.confirm_panel_mode(DSC)
        self.send_panel_state(DSC, ready=True)
        self.device.writes.clear()
        self.send_panel_action(bridge, "arm_night")

        self.confirm_panel_mode(ADEMCO)
        self.send_panel_state(ADEMCO)
        self.send_panel_action(bridge, "disarm")

        self.assertListEqual(self.device.writes, [b"*90000"])
        self.assertEqual(bridge._dsc_action_in_flight, "arm_night")

    def test_panel_actions_use_panel_specific_wire_sequences(self):
        expected_by_mode = {
            ADEMCO: {
                "arm_away": b"00002",
                "arm_stay": b"00003",
                "arm_night": b"00007",
                "disarm": b"00001",
            },
            DSC: {
                "arm_away": b"\x05\x05\x05",
                "arm_stay": b"\x04\x04\x04",
                "arm_night": b"*90000",
                "disarm": b"0000",
            },
        }

        for mode, expected_actions in expected_by_mode.items():
            for action, expected in expected_actions.items():
                with self.subTest(mode=mode, action=action):
                    self.reset_harness()
                    bridge = self.make_bridge()
                    self.confirm_panel_mode(mode)
                    if mode == DSC:
                        self.send_panel_state(
                            DSC,
                            armed_away=(action == "disarm"),
                            ready=(action != "disarm"),
                        )
                    self.device.writes.clear()
                    self.send_panel_action(bridge, action)
                    self.assertListEqual(self.device.writes, [expected])

    def test_dsc_actions_require_state_appropriate_to_the_action(self):
        bridge = self.make_bridge()
        self.confirm_panel_mode(DSC)

        self.send_panel_state(DSC, ready=True)
        self.device.writes.clear()
        self.send_panel_action(bridge, "disarm")
        self.assertListEqual(self.device.writes, [])

        self.send_panel_state(DSC, armed_away=True)
        self.device.writes.clear()
        for action in ("arm_away", "arm_stay", "arm_night"):
            with self.subTest(action=action):
                self.send_panel_action(bridge, action)
                self.assertListEqual(self.device.writes, [])

        self.send_panel_state(DSC, ready=False)
        self.device.writes.clear()
        self.send_panel_action(bridge, "arm_away")
        self.assertListEqual(self.device.writes, [])

        self.send_panel_state(DSC, ready=True, zone_bypassed=True)
        self.device.writes.clear()
        self.send_panel_action(bridge, "arm_away")
        self.assertListEqual(self.device.writes, [])

    def test_dsc_night_arm_duplicate_cannot_toggle_entry_delay_twice(self):
        bridge = self.make_bridge()
        self.confirm_panel_mode(DSC)
        self.send_panel_state(DSC, ready=True)
        self.device.writes.clear()

        self.send_panel_action(bridge, "arm_night")
        self.send_panel_action(bridge, "arm_night")
        self.send_panel_state(DSC, ready=True)
        self.send_panel_action(bridge, "arm_night")
        self.assertListEqual(self.device.writes, [b"*90000"])

        self.send_panel_state(
            DSC,
            armed_home=True,
            entry_delay_off=True,
        )
        self.assertIsNone(bridge._dsc_action_in_flight)
        self.send_panel_action(bridge, "arm_night")
        self.assertListEqual(self.device.writes, [b"*90000"])

        self.send_panel_state(DSC, ready=True)
        self.send_panel_action(bridge, "arm_away")
        self.assertListEqual(
            self.device.writes,
            [b"*90000", b"\x05\x05\x05"],
        )

    def test_dsc_disarm_duplicate_waits_for_non_alarming_disarmed_kpm(self):
        bridge = self.make_bridge()
        self.confirm_panel_mode(DSC)
        self.send_panel_state(DSC, armed_away=True)
        self.send_panel_state(DSC, alarm_sounding=True, numeric_code="002")
        self.device.writes.clear()

        self.send_panel_action(bridge, "disarm")
        self.send_panel_state(DSC, alarm_sounding=True, numeric_code="002")
        self.send_panel_action(bridge, "disarm")
        self.assertListEqual(self.device.writes, [b"0000"])

        self.send_panel_state(DSC)
        self.assertIsNone(bridge._dsc_action_in_flight)
        self.send_panel_action(bridge, "disarm")
        self.assertListEqual(self.device.writes, [b"0000"])

        self.send_panel_state(DSC, armed_away=True)
        self.send_panel_action(bridge, "disarm")
        self.assertListEqual(self.device.writes, [b"0000", b"0000"])

    def test_unexpected_terminal_arm_state_does_not_block_disarm(self):
        bridge = self.make_bridge()
        self.confirm_panel_mode(DSC)
        self.send_panel_state(DSC, ready=True)
        self.device.writes.clear()

        self.send_panel_action(bridge, "arm_night")
        self.send_panel_state(DSC, armed_home=True, entry_delay_off=False)
        self.assertIsNone(bridge._dsc_action_in_flight)
        self.send_panel_action(bridge, "disarm")
        self.assertListEqual(self.device.writes, [b"*90000", b"0000"])

        self.reset_harness()
        bridge = self.make_bridge()
        self.confirm_panel_mode(DSC)
        self.send_panel_state(DSC, ready=True)
        self.device.writes.clear()
        self.send_panel_action(bridge, "arm_away")
        self.send_panel_state(DSC, alarm_sounding=True, numeric_code="002")
        self.assertIsNone(bridge._dsc_action_in_flight)
        self.send_panel_action(bridge, "disarm")
        self.assertListEqual(
            self.device.writes,
            [b"\x05\x05\x05", b"0000"],
        )

    def test_chime_uses_panel_specific_toggle_sequence(self):
        for mode, expected in ((ADEMCO, b"00009"), (DSC, b"*4")):
            with self.subTest(mode=mode):
                self.reset_harness()
                bridge = self.make_bridge()
                self.confirm_panel_mode(mode)
                self.send_panel_state(mode, chime_on=False)
                self.device.writes.clear()
                bridge.cb_chime_set(None, None, self.mqtt_message("ON"))
                self.assertListEqual(self.device.writes, [expected])

    def test_chime_request_survives_lifecycle_until_fresh_kpm(self):
        bridge = self.make_bridge()
        bridge.cb_chime_set(None, None, self.mqtt_message("ON"))
        self.assertListEqual(self.device.writes, [])

        self.device.on_open()
        self.assertListEqual(self.device.writes, [b"C\r", b"V\r"])
        self.confirm_panel_mode(DSC)
        self.device.writes.clear()
        self.send_panel_state(DSC, chime_on=False)
        self.assertListEqual(self.device.writes, [b"*4"])

    def test_chime_toggle_is_not_repeated_by_qos_or_stale_kpm(self):
        bridge = self.make_bridge()
        self.confirm_panel_mode(DSC)
        self.send_panel_state(DSC, chime_on=False)
        self.device.writes.clear()

        bridge.cb_chime_set(None, None, self.mqtt_message("ON"))
        bridge.cb_chime_set(None, None, self.mqtt_message("ON"))
        self.send_panel_state(DSC, chime_on=False)
        self.confirm_panel_mode(DSC)
        bridge.cb_chime_set(None, None, self.mqtt_message("ON"))
        self.assertListEqual(self.device.writes, [b"*4"])

        self.send_panel_state(DSC, chime_on=True)
        self.assertIsNone(bridge._chime_toggle_target)
        bridge.cb_chime_set(None, None, self.mqtt_message("ON"))
        self.assertListEqual(self.device.writes, [b"*4"])

        bridge.cb_chime_set(None, None, self.mqtt_message("OFF"))
        self.assertListEqual(self.device.writes, [b"*4", b"*4"])

    def test_latest_chime_intent_runs_after_in_flight_target_arrives(self):
        bridge = self.make_bridge()
        self.confirm_panel_mode(DSC)
        self.send_panel_state(DSC, chime_on=False)
        self.device.writes.clear()

        bridge.cb_chime_set(None, None, self.mqtt_message("ON"))
        bridge.cb_chime_set(None, None, self.mqtt_message("OFF"))
        self.assertListEqual(self.device.writes, [b"*4"])

        self.send_panel_state(DSC, chime_on=True)
        self.assertListEqual(self.device.writes, [b"*4", b"*4"])
        self.assertEqual(bridge._chime_toggle_target, False)
        self.send_panel_state(DSC, chime_on=False)
        self.assertIsNone(bridge._chime_toggle_target)

    def test_programming_mode_is_telemetry_only_for_every_command_path(self):
        zone = FakeZone(2, "faulted_zone")
        bridge = self.make_bridge(zones={2: zone})
        self.confirm_panel_mode(DSC)

        # Intents received before a programming KPM must not survive it.
        bridge.cb_bypass_set(None, None, self.mqtt_message("ON"))
        bridge.cb_chime_set(None, None, self.mqtt_message("ON"))
        self.assertTrue(bridge.is_bypass)
        self.assertEqual(bridge._deferred_chime_target, True)
        self.send_panel_state(
            DSC,
            programming_mode=True,
            ready=True,
        )
        bridge.on_zone_fault(self.decoder, 2)
        bridge.cb_bypass_set(None, None, self.mqtt_message("ON"))
        self.device.writes.clear()

        for action in ("arm_away", "arm_stay", "arm_night", "disarm"):
            self.send_panel_action(bridge, action)
        bridge.cb_chime_set(None, None, self.mqtt_message("ON"))

        self.assertFalse(bridge._panel_state_confirmed)
        self.assertFalse(bridge.is_bypass)
        self.assertIsNone(bridge._deferred_chime_target)
        self.assertListEqual(self.device.writes, [])
        self.assertEqual(len(self.publications_for(bridge.panel_msg_topic)), 1)

        # Returning to normal keypad state must not execute latent commands.
        self.send_panel_state(DSC, ready=True)
        self.assertFalse(bridge._panel_programming_mode)
        self.assertListEqual(self.device.writes, [])

    def test_programming_veto_is_sticky_and_applies_to_vista(self):
        bridge = self.make_bridge()
        bridge.cb_chime_set(None, None, self.mqtt_message("ON"))
        self.send_panel_state(
            DSC,
            programming_mode=True,
            text="Hit * for faults",
        )
        self.assertListEqual(self.device.writes, [])
        self.assertListEqual(self.device.blocked_writes, [b"*"])
        self.assertTrue(bridge._panel_programming_mode)
        self.assertIsNone(bridge._deferred_chime_target)

        self.confirm_panel_mode(DSC)
        self.send_panel_state(ADEMCO, ready=True)
        self.assertTrue(bridge._panel_programming_mode)
        bridge.cb_bypass_set(None, None, self.mqtt_message("ON"))
        bridge.cb_chime_set(None, None, self.mqtt_message("ON"))
        self.assertFalse(bridge.is_bypass)
        self.assertIsNone(bridge._deferred_chime_target)

        self.send_panel_state(DSC, ready=True)
        self.assertFalse(bridge._panel_programming_mode)
        self.assertListEqual(self.device.writes, [])

        self.reset_harness()
        bridge = self.make_bridge()
        self.confirm_panel_mode(ADEMCO)
        self.send_panel_state(ADEMCO, programming_mode=True, ready=True)
        self.device.writes.clear()
        self.send_panel_action(bridge, "arm_away")
        bridge.cb_chime_set(None, None, self.mqtt_message("ON"))
        bridge.cb_bypass_set(None, None, self.mqtt_message("ON"))
        self.send_panel_state(ADEMCO, ready=True)
        self.assertListEqual(self.device.writes, [])
        self.assertFalse(bridge.is_bypass)
        self.assertIsNone(bridge._deferred_chime_target)

    def test_untransmitted_action_and_chime_are_canceled_by_a_kpm(self):
        bridge = self.make_bridge()
        self.confirm_panel_mode(DSC)
        self.send_panel_state(DSC, armed_away=True, chime_on=False)
        self.device.writes.clear()
        self.device.defer_authorized_writes = True

        self.send_panel_action(bridge, "disarm")
        self.assertEqual(len(self.device.queued_writes), 1)
        self.assertFalse(bridge._dsc_action_transmitted)
        self.send_panel_state(DSC, chime_on=False)
        self.assertListEqual(self.device.queued_writes, [])
        self.assertIsNone(bridge._dsc_action_in_flight)
        self.device.flush_writes()
        self.assertListEqual(self.device.writes, [])

        bridge.cb_chime_set(None, None, self.mqtt_message("ON"))
        self.assertEqual(len(self.device.queued_writes), 1)
        self.assertFalse(bridge._chime_transmitted)
        self.send_panel_state(DSC, chime_on=True)
        self.assertListEqual(self.device.queued_writes, [])
        self.assertIsNone(bridge._chime_toggle_target)
        self.device.flush_writes()
        self.assertListEqual(self.device.writes, [])

    def test_untransmitted_vista_action_is_canceled_on_safety_transition(self):
        for transition in ("mode_change", "programming_kpm"):
            with self.subTest(transition=transition):
                self.reset_harness()
                bridge = self.make_bridge()
                self.confirm_panel_mode(ADEMCO)
                self.device.defer_authorized_writes = True
                self.device.writes.clear()

                self.send_panel_action(bridge, "disarm")
                self.assertEqual(len(self.device.queued_writes), 1)
                self.assertIsNotNone(bridge._action_write_token)
                self.assertFalse(bridge._action_transmitted)

                if transition == "mode_change":
                    self.confirm_panel_mode(DSC)
                else:
                    self.send_panel_state(
                        ADEMCO,
                        programming_mode=True,
                    )

                self.assertListEqual(self.device.queued_writes, [])
                self.assertIsNone(bridge._action_write_token)
                self.device.flush_writes()
                self.assertListEqual(self.device.writes, [])

    def test_completion_requires_ad2_ack_and_a_later_kpm(self):
        bridge = self.make_bridge()
        self.confirm_panel_mode(DSC)
        self.send_panel_state(DSC, armed_away=True, chime_on=False)
        self.device.writes.clear()

        self.send_panel_action(bridge, "disarm")
        self.assertTrue(bridge._dsc_action_transmitted)
        self.assertFalse(bridge._dsc_action_acknowledged)
        self.send_panel_state(DSC, acknowledge=False, chime_on=False)
        self.assertEqual(bridge._dsc_action_in_flight, "disarm")

        self.device.acknowledge_writes(count=1)
        self.assertFalse(bridge._dsc_action_acknowledged)
        self.assertEqual(bridge._action_ack_remaining, 3)
        self.send_panel_state(DSC, acknowledge=False, chime_on=False)
        self.assertEqual(bridge._dsc_action_in_flight, "disarm")

        self.device.acknowledge_writes()
        self.assertTrue(bridge._dsc_action_acknowledged)
        self.assertEqual(bridge._dsc_action_in_flight, "disarm")
        self.send_panel_state(DSC, acknowledge=False, chime_on=False)
        self.assertIsNone(bridge._dsc_action_in_flight)

        bridge.cb_chime_set(None, None, self.mqtt_message("ON"))
        self.send_panel_state(
            DSC,
            acknowledge=False,
            chime_on=True,
        )
        self.assertEqual(bridge._chime_toggle_target, True)
        self.device.acknowledge_writes(count=1)
        self.assertFalse(bridge._chime_acknowledged)
        self.assertEqual(bridge._chime_ack_remaining, 1)
        self.send_panel_state(
            DSC,
            acknowledge=False,
            chime_on=True,
        )
        self.assertEqual(bridge._chime_toggle_target, True)

        self.device.acknowledge_writes()
        self.assertEqual(bridge._chime_toggle_target, True)
        self.send_panel_state(
            DSC,
            acknowledge=False,
            chime_on=True,
        )
        self.assertIsNone(bridge._chime_toggle_target)

    def test_vista_action_serializes_chime_until_every_key_is_acked(self):
        bridge = self.make_bridge()
        self.confirm_panel_mode(ADEMCO)
        self.send_panel_state(ADEMCO, chime_on=False)
        self.device.writes.clear()

        self.send_panel_action(bridge, "disarm")
        bridge.cb_chime_set(None, None, self.mqtt_message("ON"))
        self.assertListEqual(self.device.writes, [b"00001"])
        self.assertEqual(bridge._deferred_chime_target, True)

        self.device.acknowledge_writes(count=1)
        self.assertIsNotNone(bridge._action_write_token)
        self.assertEqual(bridge._action_ack_remaining, 4)
        bridge.cb_chime_set(None, None, self.mqtt_message("ON"))
        self.assertListEqual(self.device.writes, [b"00001"])

        self.device.acknowledge_writes()
        self.assertIsNone(bridge._action_write_token)
        self.send_panel_state(
            ADEMCO,
            acknowledge=False,
            chime_on=False,
        )
        self.assertListEqual(self.device.writes, [b"00001", b"00009"])

    def test_failed_key_ack_keeps_the_action_interlocked(self):
        bridge = self.make_bridge()
        self.confirm_panel_mode(ADEMCO)
        self.device.writes.clear()

        self.send_panel_action(bridge, "disarm")
        self.device.acknowledge_writes(status=False, count=1)
        self.device.acknowledge_writes()
        self.assertTrue(bridge._action_ack_failed)
        self.assertFalse(bridge._action_acknowledged)
        self.assertIsNotNone(bridge._action_write_token)

        self.send_panel_action(bridge, "arm_away")
        bridge.cb_chime_set(None, None, self.mqtt_message("ON"))
        self.assertListEqual(self.device.writes, [b"00001"])
        self.assertEqual(bridge._deferred_chime_target, True)

    def test_ambiguous_partial_action_blocks_follow_up_disarm(self):
        bridge = self.make_bridge()
        self.confirm_panel_mode(DSC)
        self.send_panel_state(DSC, ready=True)
        self.device.defer_authorized_writes = True
        self.device.uncancelable_writes = True
        self.device.writes.clear()

        self.send_panel_action(bridge, "arm_night")
        self.send_panel_state(
            DSC,
            acknowledge=False,
            armed_away=True,
        )
        self.assertEqual(bridge._dsc_action_in_flight, "arm_night")
        self.send_panel_action(bridge, "disarm")

        self.assertListEqual(self.device.queued_writes, [])
        self.assertListEqual(self.device.writes, [])
        self.assertEqual(bridge._dsc_action_in_flight, "arm_night")

    def test_bypass_sequences_are_one_shot_and_never_prefix_disarm(self):
        faulted_2 = FakeZone(2, "faulted_2")
        faulted_12 = FakeZone(12, "faulted_12")
        clear_5 = FakeZone(5, "clear_5")
        unknown_7 = FakeZone(7, "unknown_7")
        zones = {
            12: faulted_12,
            5: clear_5,
            2: faulted_2,
            7: unknown_7,
        }

        bridge = self.make_bridge(zones=zones)
        self.confirm_panel_mode(ADEMCO)
        faulted_2.faulted = True
        bridge.cb_bypass_set(None, None, self.mqtt_message("ON"))
        self.device.writes.clear()
        self.send_panel_action(bridge, "arm_away")
        self.assertListEqual(self.device.writes, [b"00006#00002"])
        self.assertFalse(bridge.is_bypass)

        self.reset_harness()
        bridge = self.make_bridge(zones=zones)
        self.confirm_panel_mode(DSC)
        self.send_panel_state(DSC)
        bridge.on_zone_fault(self.decoder, 2)
        bridge.on_zone_fault(self.decoder, 12)
        bridge.on_zone_restore(self.decoder, 5)
        unknown_7.faulted = None
        bridge.cb_bypass_set(None, None, self.mqtt_message("ON"))
        self.device.writes.clear()
        self.send_panel_action(bridge, "arm_away")
        self.assertListEqual(
            self.device.writes,
            [b"*10212#\x05\x05\x05"],
        )
        self.assertFalse(bridge.is_bypass)

        self.send_panel_state(DSC, armed_away=True, zone_bypassed=True)
        bridge.cb_bypass_set(None, None, self.mqtt_message("ON"))
        self.device.writes.clear()
        self.send_panel_action(bridge, "disarm")
        self.assertListEqual(self.device.writes, [b"0000"])

    def test_dsc_bypass_arm_duplicate_is_blocked_until_confirmed(self):
        faulted_zone = FakeZone(2, "faulted_zone")
        bridge = self.make_bridge(zones={2: faulted_zone})
        self.confirm_panel_mode(DSC)
        self.send_panel_state(DSC)
        bridge.on_zone_fault(self.decoder, 2)
        bridge.cb_bypass_set(None, None, self.mqtt_message("ON"))
        self.device.writes.clear()

        self.send_panel_action(bridge, "arm_away")
        self.send_panel_action(bridge, "arm_away")
        self.send_panel_state(DSC)
        self.send_panel_action(bridge, "arm_away")
        self.assertListEqual(
            self.device.writes,
            [b"*102#\x05\x05\x05"],
        )

        self.send_panel_state(DSC, armed_away=True, zone_bypassed=True)
        self.send_panel_state(DSC)
        bridge.on_zone_fault(self.decoder, 2)
        bridge.cb_bypass_set(None, None, self.mqtt_message("ON"))
        self.send_panel_action(bridge, "arm_away")
        self.assertListEqual(
            self.device.writes,
            [b"*102#\x05\x05\x05", b"*102#\x05\x05\x05"],
        )

    def test_dsc_bypass_rejects_unsafe_target_states(self):
        cases = (
            (FakeZone(2, "clear_zone"), False, False),
            (FakeZone(0, "invalid_zone"), True, False),
            (FakeZone(2, "already_bypassed"), True, True),
        )

        for zone, faulted, already_bypassed in cases:
            with self.subTest(zone=zone.zone, bypassed=already_bypassed):
                self.reset_harness()
                bridge = self.make_bridge(zones={zone.zone: zone})
                self.confirm_panel_mode(DSC)
                self.send_panel_state(
                    DSC,
                    zone_bypassed=already_bypassed,
                )
                if faulted:
                    bridge.on_zone_fault(self.decoder, zone.zone)
                else:
                    bridge.on_zone_restore(self.decoder, zone.zone)
                bridge.cb_bypass_set(None, None, self.mqtt_message("ON"))
                self.device.writes.clear()
                self.send_panel_action(bridge, "arm_away")
                self.assertListEqual(self.device.writes, [])
                self.assertFalse(bridge.is_bypass)

    def test_bypass_intent_does_not_survive_rejection_or_mode_change(self):
        zone = FakeZone(2, "faulted_zone")
        bridge = self.make_bridge(zones={2: zone})
        self.confirm_panel_mode(DSC)
        self.send_panel_state(DSC, ready=True)
        bridge.cb_bypass_set(None, None, self.mqtt_message("ON"))
        self.device.writes.clear()

        self.send_panel_action(bridge, "arm_away")
        self.assertFalse(bridge.is_bypass)
        bridge.on_zone_fault(self.decoder, 2)
        self.send_panel_action(bridge, "arm_away")
        self.assertListEqual(self.device.writes, [b"\x05\x05\x05"])

        self.reset_harness()
        bridge = self.make_bridge(zones={2: zone})
        self.confirm_panel_mode(DSC)
        self.send_panel_state(DSC, ready=True)
        bridge.cb_bypass_set(None, None, self.mqtt_message("ON"))
        self.confirm_panel_mode(ADEMCO)
        self.assertFalse(bridge.is_bypass)
        self.device.writes.clear()
        self.send_panel_action(bridge, "arm_away")
        self.assertListEqual(self.device.writes, [b"00002"])

    def test_bypass_intent_does_not_survive_mqtt_disconnect(self):
        zone = FakeZone(2, "faulted_zone")
        bridge = self.make_bridge(zones={2: zone})
        self.confirm_panel_mode(DSC)
        self.send_panel_state(DSC, ready=True)
        bridge.on_zone_fault(self.decoder, 2)
        bridge.cb_bypass_set(None, None, self.mqtt_message("ON"))
        self.assertTrue(bridge.is_bypass)

        bridge.mqtt_connected(self.mqtt, False)
        self.assertFalse(bridge.is_bypass)
        bridge.mqtt_connected(self.mqtt, True)
        bypass_states = self.publications_for(bridge.bypass_state_topic)
        self.assertEqual(bypass_states[-1]["payload"]["status"], "OFF")

        bridge.cb_bypass_set(None, None, self.mqtt_message("ON"))
        bridge.mqtt_connected(self.mqtt, True)
        self.assertFalse(bridge.is_bypass)
        bypass_states = self.publications_for(bridge.bypass_state_topic)
        self.assertEqual(bypass_states[-1]["payload"]["status"], "OFF")

        self.device.writes.clear()
        self.send_panel_action(bridge, "arm_away")
        self.assertListEqual(self.device.writes, [b"\x05\x05\x05"])

    def test_retained_bypass_on_is_rejected_and_not_discovered(self):
        bridge = self.make_bridge()
        bridge.cb_bypass_set(
            None,
            None,
            self.mqtt_message("ON", retain=True),
        )
        self.assertFalse(bridge.is_bypass)
        bridge.cb_bypass_set(
            None,
            None,
            self.mqtt_message("ON", dup=True),
        )
        self.assertFalse(bridge.is_bypass)
        bridge.cb_bypass_set(None, None, self.mqtt_message("ON"))
        self.assertTrue(bridge.is_bypass)
        bridge.cb_bypass_set(
            None,
            None,
            self.mqtt_message("OFF", dup=True),
        )
        self.assertFalse(bridge.is_bypass)

        tree = ast.parse(
            (REPO_ROOT / "ad_mqtt" / "Discovery.py").read_text()
        )
        bypass_command_dicts = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            keys = [
                key.value if isinstance(key, ast.Constant) else None
                for key in node.keys
            ]
            values = dict(zip(keys, node.values))
            command_topic = values.get("command_topic")
            if (isinstance(command_topic, ast.Attribute)
                    and command_topic.attr == "bypass_set_topic"):
                bypass_command_dicts.append(values)

        self.assertEqual(len(bypass_command_dicts), 1)
        self.assertNotIn("retain", bypass_command_dicts[0])

    def test_retained_or_duplicate_one_shot_panel_actions_are_rejected(self):
        bridge = self.make_bridge()
        self.confirm_panel_mode(DSC)
        self.send_panel_state(DSC, ready=True)
        payload = json.dumps({"action": "arm_night", "code": "0000"})
        self.device.writes.clear()

        bridge.cb_panel_set(
            None,
            None,
            self.mqtt_message(payload, retain=True),
        )
        bridge.cb_panel_set(
            None,
            None,
            self.mqtt_message(payload, dup=True),
        )
        bridge.cb_chime_set(
            None,
            None,
            self.mqtt_message("ON", retain=True),
        )

        self.assertListEqual(self.device.writes, [])

    def test_invalid_switch_payloads_cannot_create_physical_intent(self):
        bridge = self.make_bridge()
        self.confirm_panel_mode(DSC)
        self.send_panel_state(DSC, chime_on=True, ready=True)
        self.device.writes.clear()

        bridge.cb_chime_set(None, None, self.mqtt_message("invalid"))
        bridge.cb_bypass_set(None, None, self.mqtt_message("invalid"))

        self.assertListEqual(self.device.writes, [])
        self.assertIsNone(bridge._deferred_chime_target)
        self.assertIsNone(bridge._chime_toggle_target)
        self.assertFalse(bridge.is_bypass)

    def test_pre_config_expander_fault_cannot_target_wrong_dsc_zone(self):
        vista_zone = FakeZone(9, "vista_zone")
        dsc_zone = FakeZone(57, "dsc_zone")
        bridge = self.make_bridge(zones={9: vista_zone, 57: dsc_zone})

        # Address 7/channel 1 maps to Vista zone 9 while the decoder is still
        # in its default mode, but to DSC zone 57 after MODE=D is confirmed.
        self.device.on_read(data=b"!EXP:07,01,01")
        self.assertIsNone(vista_zone.faulted)
        self.assertListEqual(
            self.publications_for(bridge.sensor_state_topic),
            [],
        )

        self.confirm_panel_mode(DSC)
        self.send_panel_state(DSC)
        bridge.cb_bypass_set(None, None, self.mqtt_message("ON"))
        self.device.writes.clear()
        self.send_panel_action(bridge, "arm_away")
        self.assertListEqual(self.device.writes, [])

        self.device.on_read(data=b"!EXP:07,01,01")
        self.assertIsNone(vista_zone.faulted)
        self.assertTrue(dsc_zone.faulted)
        bridge.cb_bypass_set(None, None, self.mqtt_message("ON"))
        self.send_panel_action(bridge, "arm_away")
        self.assertListEqual(
            self.device.writes,
            [b"*157#\x05\x05\x05"],
        )

    def test_pre_config_expander_state_is_applied_when_mode_matches(self):
        zone = FakeZone(9, "vista_zone")
        bridge = self.make_bridge(zones={9: zone})

        self.device.on_read(data=b"!EXP:07,01,01")
        self.assertIsNone(zone.faulted)
        self.assertListEqual(
            self.publications_for(bridge.sensor_state_topic),
            [],
        )

        self.confirm_panel_mode(ADEMCO)
        self.assertTrue(zone.faulted)
        self.assertTrue(bridge._command_zone_states[9])
        states = self.publications_for(
            bridge.sensor_state_topic.format(entity="vista_zone")
        )
        self.assertListEqual(
            [publication["payload"]["status"] for publication in states],
            ["ON"],
        )

    def test_expander_state_reconciles_across_reconnect_orderings(self):
        zone = FakeZone(57, "dsc_zone")
        bridge = self.make_bridge(zones={57: zone})
        self.confirm_panel_mode(DSC)
        self.device.on_read(data=b"!EXP:07,01,01")
        self.assertTrue(zone.faulted)

        self.device.on_close()
        self.device.on_open()
        self.device.on_read(data=b"!EXP:07,01,01")
        self.assertNotIn(57, bridge._command_zone_states)
        self.confirm_panel_mode(DSC)
        self.assertTrue(bridge._command_zone_states[57])

        self.device.on_close()
        self.device.on_open()
        self.confirm_panel_mode(DSC)
        self.assertNotIn(57, bridge._command_zone_states)
        self.device.on_read(data=b"!EXP:07,01,01")
        self.assertTrue(bridge._command_zone_states[57])

        self.device.on_close()
        self.device.on_open()
        self.device.on_read(data=b"!EXP:07,01,00")
        self.confirm_panel_mode(DSC)
        self.assertFalse(zone.faulted)
        self.assertFalse(bridge._command_zone_states[57])

    def test_restore_all_zone_telemetry_survives_first_config(self):
        zone = FakeZone(2, "front_door")
        bridge = self.make_bridge(zones={2: zone})

        bridge.reset_all_zones()
        self.device.on_open()
        self.confirm_panel_mode(DSC)

        self.assertFalse(zone.faulted)

    def test_panel_command_logs_do_not_contain_alarm_codes(self):
        bridge = self.make_bridge()
        self.confirm_panel_mode(ADEMCO)

        with self.assertLogs(level="INFO") as captured:
            self.send_panel_action(bridge, "arm_away")
            self.send_panel_action(bridge, "disarm", code="9876")

        log_text = "\n".join(captured.output)
        self.assertNotIn("0000", log_text)
        self.assertNotIn("9876", log_text)
        self.assertNotIn("00002", log_text)

        client_tree = ast.parse(
            (REPO_ROOT / "ad_mqtt" / "Client.py").read_text()
        )
        write_method = next(
            node
            for node in ast.walk(client_tree)
            if isinstance(node, ast.FunctionDef) and node.name == "write"
        )
        debug_calls = [
            node
            for node in ast.walk(write_method)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "debug"
        ]
        for call in debug_calls:
            self.assertFalse(
                any(
                    isinstance(argument, ast.Name)
                    and argument.id == "data"
                    for argument in call.args[1:]
                )
            )

    def test_panel_message_flows_from_device_to_mqtt(self):
        bridge = self.make_bridge()
        self.confirm_panel_mode(ADEMCO)
        received = []
        self.decoder.on_message += (
            lambda sender, message: received.append((sender, message))
        )

        self.device.on_read(data=PANEL_MESSAGE)

        self.assertEqual(len(received), 1)
        sender, message = received[0]
        self.assertIs(sender, self.decoder)
        self.assertIsInstance(message, Message)
        self.assertEqual(message.panel_type, ADEMCO)

        expected_attributes = {
            "ac_power",
            "alarm_event_occurred",
            "alarm_sounding",
            "armed_away",
            "armed_home",
            "backlight_on",
            "battery_low",
            "check_zone",
            "chime_on",
            "entry_delay_off",
            "programming_mode",
            "ready",
            "text",
            "zone_bypassed",
        }
        self.assertTrue(
            all(hasattr(message, name) for name in expected_attributes)
        )

        panel_messages = self.publications_for(bridge.panel_msg_topic)
        panel_states = self.publications_for(bridge.panel_state_topic)
        self.assertEqual(len(panel_messages), 1)
        self.assertEqual(panel_messages[0]["payload"]["status"], "")
        self.assertEqual(len(panel_states), 1)
        self.assertEqual(panel_states[0]["payload"]["status"], "disarmed")
        self.assertDictEqual(
            panel_states[0]["payload"]["attr"],
            {
                "ac_power_on": False,
                "alarm_event_occurred": False,
                "backlight_on": False,
                "battery_low": False,
                "check_zone": False,
                "chime_on": False,
                "entry_delay_off": False,
                "programming_mode": False,
                "ready": False,
                "zone_bypassed": False,
            },
        )

    def test_sequential_rf_messages_restore_loop_and_publish_state(self):
        zone = FakeZone(25, "front_door")
        bridge = self.make_bridge(
            zones={25: zone},
            rf_devices={"0180036": FakeRfDevice(zone)},
        )
        received = []
        self.decoder.on_rfx_message += (
            lambda sender, message: received.append((sender, message))
        )

        self.device.on_read(data=b"!RFX:0180036,82")
        self.device.on_read(data=b"!RFX:0180036,00")

        self.assertEqual(len(received), 2)
        self.assertTrue(all(sender is self.decoder for sender, _ in received))
        self.assertTrue(
            all(isinstance(message, RFMessage) for _, message in received)
        )
        first, second = (message for _, message in received)
        self.assertIsNot(first.loop, second.loop)
        self.assertListEqual(first.loop, [True, False, False, False])
        self.assertListEqual(second.loop, [False, False, False, False])
        self.assertEqual(first.serial_number, "0180036")
        self.assertEqual(first.value, 0x82)
        self.assertTrue(first.battery)
        self.assertFalse(first.supervision)
        self.assertFalse(zone.faulted)

        states = self.publications_for(
            bridge.sensor_state_topic.format(entity="front_door")
        )
        batteries = self.publications_for(
            bridge.sensor_battery_topic.format(entity="front_door")
        )
        self.assertListEqual(
            [publication["payload"]["status"] for publication in states],
            ["ON", "OFF"],
        )
        self.assertListEqual(
            [publication["payload"]["status"] for publication in batteries],
            [10, 100],
        )

    def test_identical_rf_report_refreshes_command_trust_after_reconnect(self):
        zone = FakeZone(25, "front_door")
        bridge = self.make_bridge(
            zones={25: zone},
            rf_devices={"0180036": FakeRfDevice(zone)},
        )
        self.confirm_panel_mode(DSC)
        self.device.on_read(data=b"!RFX:0180036,82")
        state_topic = bridge.sensor_state_topic.format(entity="front_door")
        initial_publications = len(self.publications_for(state_topic))
        self.assertTrue(bridge._command_zone_states[25])

        self.device.on_close()
        self.device.on_open()
        self.confirm_panel_mode(DSC)
        self.assertNotIn(25, bridge._command_zone_states)
        self.device.on_read(data=b"!RFX:0180036,82")

        self.assertTrue(bridge._command_zone_states[25])
        self.assertEqual(
            len(self.publications_for(state_topic)),
            initial_publications,
        )
