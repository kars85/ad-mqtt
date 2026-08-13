import ast
import inspect
import json
import runpy
from pathlib import Path
from unittest import TestCase

from alarmdecoder import AlarmDecoder
from alarmdecoder.event import event
from alarmdecoder.event.event import Event
from alarmdecoder.messages import Message, RFMessage

REPO_ROOT = Path(__file__).resolve().parents[1]

# Importing ad_mqtt currently imports the legacy insteon-mqtt transport eagerly.
# Load the dependency-free Bridge module directly so these contract tests exercise
# AlarmDecoder integration without requiring a broker, ser2sock, or that transport.
Bridge = runpy.run_path(str(REPO_ROOT / "ad_mqtt" / "Bridge.py"))["Bridge"]


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
}


class FakeDevice:
    on_open = event.Event()
    on_close = event.Event()
    on_read = event.Event()
    on_write = event.Event()

    def __init__(self):
        self.writes = []

    def write(self, data):
        self.writes.append(data)
        self.on_write(data=data)


class FakeSignal:
    def __init__(self):
        self.callbacks = []

    def connect(self, callback):
        self.callbacks.append(callback)


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
        self.device = FakeDevice()
        self.decoder = AlarmDecoder(self.device)
        self.decoder.wire_events()
        self.mqtt = FakeMqtt()

    def make_bridge(self, zones=None, rf_devices=None):
        return Bridge(
            self.mqtt,
            self.decoder,
            "0000",
            zones or {},
            rf_devices or {},
        )

    def publications_for(self, topic):
        return [
            publication
            for publication in self.mqtt.publications
            if publication["topic"] == topic
        ]

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
        self.assertEqual(len(subscribed_events), 21)

        for event_name, (callback_name, signature) in EVENT_CALLBACKS.items():
            callback = getattr(bridge, callback_name)
            self.assertListEqual(list(getattr(self.decoder, event_name)), [callback])
            self.assertEqual(str(inspect.signature(callback)), signature)

    def test_panel_message_flows_from_device_to_mqtt(self):
        bridge = self.make_bridge()
        received = []
        self.decoder.on_message += (
            lambda sender, message: received.append((sender, message))
        )

        self.device.on_read(
            data=(
                b'!KPM:[00000000000000000A--],000,'
                b'[f707000600e5800c0c020000],"                                "'
            )
        )

        self.assertEqual(len(received), 1)
        sender, message = received[0]
        self.assertIs(sender, self.decoder)
        self.assertIsInstance(message, Message)

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
        self.assertTrue(all(hasattr(message, name) for name in expected_attributes))

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
        self.assertTrue(all(isinstance(message, RFMessage) for _, message in received))
        first, second = (message for _, message in received)
        self.assertIsNot(first.loop, second.loop)
        self.assertListEqual(first.loop, [True, False, False, False])
        self.assertListEqual(second.loop, [False, False, False, False])
        self.assertEqual(first.serial_number, "0180036")
        self.assertEqual(first.value, 0x82)
        self.assertTrue(first.battery)
        self.assertFalse(first.supervision)
        self.assertFalse(zone.faulted)

        states = self.publications_for(bridge.sensor_state_topic.format(entity="front_door"))
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
