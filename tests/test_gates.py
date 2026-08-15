"""Tests for the handoff code gates G1 (payload-free logs), G14
(liveness/in-flight monitoring), and G15 (restricted alarm-code file)."""
import asyncio
import os
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from ad_mqtt.Config import Config  # noqa: E402
from ad_mqtt import run as runtime  # noqa: E402


class AlarmCodeFileTest(unittest.TestCase):
    """G15: the alarm code comes from a restricted, regular file."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "panel-code"

    def write_code(self, code="1839", mode=0o600):
        self.path.write_text(code)
        os.chmod(self.path, mode)
        return str(self.path)

    def test_reads_code_from_restricted_file(self):
        self.assertEqual(
            Config.read_alarm_code(self.write_code("1839\n")), "1839")

    def test_rejects_group_or_other_readable_file(self):
        path = self.write_code(mode=0o640)
        with self.assertRaises(RuntimeError):
            Config.read_alarm_code(path)

    def test_rejects_symlink(self):
        self.write_code()
        link = Path(self.tmp.name) / "link"
        link.symlink_to(self.path)
        with self.assertRaises(RuntimeError):
            Config.read_alarm_code(str(link))

    def test_rejects_missing_file(self):
        with self.assertRaises(RuntimeError):
            Config.read_alarm_code(str(self.path))

    def test_rejects_empty_file(self):
        path = self.write_code("")
        with self.assertRaises(RuntimeError):
            Config.read_alarm_code(path)

    def test_runner_requires_file_interface_for_enabled_commands(self):
        runner = (REPO_ROOT / "run.py").read_text()
        self.assertIn("ADMQTT_ALARM_CODE_FILE", runner)
        self.assertIn("ADMQTT_ALARM_CODE_EXPLORATORY", runner)
        self.assertIn(
            "ADMQTT_ALARM_CODE_FILE is required when panel commands are",
            runner,
        )
        # The env-var escape hatch must be labeled exploratory-only.
        self.assertIn("exploratory", runner)


class FakeBridge:
    def __init__(self):
        self.monitor = {"in_flight": False, "ack_remaining": 0}

    def command_monitor(self):
        return dict(self.monitor)


class FakeMqtt:
    def __init__(self):
        self.publications = []

    def publish(self, topic, payload, qos=0, retain=False):
        self.publications.append((topic, payload, qos, retain))


class HeartbeatTest(unittest.IsolatedAsyncioTestCase):
    """G14: heartbeat + payload-free in-flight age / ack-remaining."""

    async def run_heartbeat(self, bridge, mqtt, path=None, beats=2):
        task = asyncio.create_task(
            runtime.heartbeat(mqtt, bridge, path=path, interval=0.01))
        try:
            while len(mqtt.publications) < beats:
                await asyncio.sleep(0.005)
        finally:
            task.cancel()

    async def test_heartbeat_publishes_and_touches_file(self):
        import json
        import tempfile
        bridge, mqtt = FakeBridge(), FakeMqtt()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "heartbeat"
            await asyncio.wait_for(
                self.run_heartbeat(bridge, mqtt, path=str(path)), timeout=5)
            self.assertTrue(path.exists())

        topic, payload, qos, retain = mqtt.publications[0]
        self.assertEqual(topic, runtime.HEARTBEAT_TOPIC)
        self.assertFalse(retain)
        body = json.loads(payload)
        self.assertEqual(
            set(body), {"time", "in_flight_age", "ack_remaining"})
        self.assertEqual(body["in_flight_age"], 0)

    async def test_stuck_in_flight_command_alerts_without_clearing(self):
        bridge, mqtt = FakeBridge(), FakeMqtt()
        bridge.monitor = {"in_flight": True, "ack_remaining": 3}
        original = runtime.IN_FLIGHT_ALERT_S
        runtime.IN_FLIGHT_ALERT_S = 0
        self.addCleanup(
            lambda: setattr(runtime, "IN_FLIGHT_ALERT_S", original))

        with self.assertLogs("ad_mqtt.run", level="WARNING") as captured:
            await asyncio.wait_for(
                self.run_heartbeat(bridge, mqtt, beats=3), timeout=5)
        alert = "\n".join(captured.output)
        self.assertIn("in flight", alert)
        self.assertIn("acknowledgements outstanding", alert)
        # Alert only: the monitor state was never mutated.
        self.assertTrue(bridge.monitor["in_flight"])
        self.assertEqual(bridge.monitor["ack_remaining"], 3)


if __name__ == "__main__":
    unittest.main()
