import asyncio
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from ad_mqtt.Client import Client  # noqa: E402
from ad_mqtt.Mqtt import Mqtt  # noqa: E402
from ad_mqtt.run import supervise  # noqa: E402


class FakeLoop:
    def __init__(self):
        self.calls = []

    def is_closed(self):
        return False

    def call_soon_threadsafe(self, callback, *args):
        self.calls.append((callback, args))


@mock.patch("ad_mqtt.Mqtt.paho.Client")
class MqttWrapperTest(unittest.TestCase):
    def test_clean_session_is_required_for_command_safety(self, client_type):
        # G9 tripwire: a persistent session would let the broker replay
        # queued commands after an outage.
        Mqtt(broker="broker.example")
        kwargs = client_type.call_args.kwargs
        self.assertTrue(kwargs["clean_session"])
        self.assertEqual(kwargs["client_id"], "ad-mqtt")

    def test_tls_applied_when_ca_cert_configured(self, client_type):
        Mqtt(broker="b", ca_cert="/ca.pem", certfile="/cert.pem",
             keyfile="/key.pem")
        client_type.return_value.tls_set.assert_called_once_with(
            ca_certs="/ca.pem", certfile="/cert.pem", keyfile="/key.pem")

    def test_tls_not_applied_without_ca_cert(self, client_type):
        Mqtt(broker="b")
        client_type.return_value.tls_set.assert_not_called()

    def test_will_and_online_availability(self, client_type):
        wrapper = Mqtt(broker="b", availability_topic="alarm/available")
        paho_client = client_type.return_value
        paho_client.will_set.assert_called_once_with(
            "alarm/available", "offline", qos=0, retain=True)

        connected = []
        wrapper.signal_connected.connect(
            lambda mqtt, status: connected.append(status))
        wrapper._on_connect(paho_client, None, None, 0, None)
        paho_client.publish.assert_called_once_with(
            "alarm/available", "online", qos=0, retain=True)
        self.assertListEqual(connected, [True])
        wrapper._on_disconnect(paho_client, None, None, 0, None)
        self.assertListEqual(connected, [True, False])

    def test_publish_passes_qos_and_retain_through(self, client_type):
        wrapper = Mqtt(broker="b")
        wrapper.publish("t", "p", qos=1, retain=True)
        client_type.return_value.publish.assert_called_once_with(
            "t", "p", 1, True)

    def test_subscribe_marshals_delivery_onto_the_loop(self, client_type):
        loop = FakeLoop()
        wrapper = Mqtt(broker="b", loop=loop)
        paho_client = client_type.return_value

        received = []
        wrapper.subscribe("cmd/topic", 1, lambda *args: received.append(args))
        paho_client.subscribe.assert_called_once_with("cmd/topic", 1)
        topic, deliver = paho_client.message_callback_add.call_args.args

        message = object()
        deliver(paho_client, None, message)
        self.assertListEqual(received, [])
        callback, args = loop.calls[0]
        callback(*args)
        self.assertListEqual(received, [(paho_client, None, message)])


class SuperviseTest(unittest.IsolatedAsyncioTestCase):
    async def test_read_write_and_reconnect_through_the_supervisor(self):
        connections = asyncio.Queue()

        async def on_connect(reader, writer):
            await connections.put((reader, writer))

        server = await asyncio.start_server(on_connect, "127.0.0.1", 0)
        self.addAsyncCleanup(server.wait_closed)
        self.addCleanup(server.close)
        port = server.sockets[0].getsockname()[1]

        client = Client("127.0.0.1", port, reconnect_dt=0.01)
        lines = []
        client.on_read += lambda sender, **kwargs: lines.append(
            kwargs["data"])

        task = asyncio.create_task(supervise(client))
        self.addCleanup(task.cancel)

        reader, writer = await asyncio.wait_for(connections.get(), timeout=5)

        # Inbound line reaches the decoder callback.
        writer.write(b"hello\r\n")
        await writer.drain()
        await asyncio.wait_for(self._until(lambda: lines), timeout=5)
        self.assertEqual(lines, [b"hello"])

        # Outbound allowed query is flushed via write readiness.
        client.write(b"C\r")
        self.assertEqual(await asyncio.wait_for(reader.read(2), timeout=5),
                         b"C\r")

        # Server drop closes the client and the supervisor reconnects.
        writer.close()
        await writer.wait_closed()
        reader, writer = await asyncio.wait_for(connections.get(), timeout=5)
        writer.write(b"again\r\n")
        await writer.drain()
        await asyncio.wait_for(self._until(lambda: len(lines) >= 2),
                               timeout=5)
        self.assertEqual(lines[-1], b"again")

    @staticmethod
    async def _until(condition):
        while not condition():
            await asyncio.sleep(0.01)

    async def test_supervisor_retries_failed_connections(self):
        client = Client("127.0.0.1", 1, reconnect_dt=0.001)
        attempts = []
        original_connect = client.connect
        client.connect = lambda: attempts.append(1) or original_connect()

        task = asyncio.create_task(supervise(client))
        self.addCleanup(task.cancel)
        await asyncio.wait_for(self._until(lambda: len(attempts) >= 3),
                               timeout=5)


if __name__ == "__main__":
    unittest.main()
