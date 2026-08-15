import logging

import paho.mqtt.client as paho

from .Signal import Signal

LOG = logging.getLogger(__name__)


class Mqtt:
    """paho-mqtt 2.x wrapper exposing the publish/subscribe/signal_connected
    surface Bridge and Discovery expect.

    paho's network thread owns the connection; every callback that reaches
    Bridge is marshaled onto the asyncio loop so Bridge stays
    single-threaded.  clean_session=True is a safety requirement (handoff
    G9): commands published while this bridge is offline must be dropped by
    the broker, never queued and replayed.
    """
    def __init__(self, broker, port=1883, username=None, password=None,
                 ca_cert=None, certfile=None, keyfile=None,
                 availability_topic="alarm/available", client_id="ad-mqtt",
                 loop=None):
        self.signal_connected = Signal()
        self.availability_topic = availability_topic
        self._addr = (broker, port)
        self._loop = loop
        self.client = paho.Client(
            paho.CallbackAPIVersion.VERSION2,
            client_id=client_id,
            clean_session=True,
        )
        if username:
            self.client.username_pw_set(username, password)
        if ca_cert:
            self.client.tls_set(ca_certs=ca_cert, certfile=certfile,
                                keyfile=keyfile)
        self.client.will_set(availability_topic, "offline", qos=0,
                             retain=True)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect

    def start(self):
        self.client.reconnect_delay_set(min_delay=1, max_delay=30)
        self.client.connect_async(*self._addr)
        self.client.loop_start()

    def stop(self):
        self.client.publish(self.availability_topic, "offline", qos=0,
                            retain=True)
        self.client.disconnect()
        self.client.loop_stop()

    def publish(self, topic, payload, qos=0, retain=False):
        self.client.publish(topic, payload, qos, retain)

    def subscribe(self, topic, qos, callback):
        def deliver(client, userdata, message):
            self._marshal(callback, client, userdata, message)

        self.client.message_callback_add(topic, deliver)
        self.client.subscribe(topic, qos)

    def _marshal(self, callback, *args):
        # Cross the paho-thread -> asyncio-loop boundary here and nowhere
        # else; Bridge's command state machine must never see two threads.
        if self._loop is None:
            callback(*args)
        elif not self._loop.is_closed():
            self._loop.call_soon_threadsafe(callback, *args)

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        LOG.info("MQTT connected: %s", reason_code)
        client.publish(self.availability_topic, "online", qos=0, retain=True)
        self._marshal(self.signal_connected.emit, self, True)

    def _on_disconnect(self, client, userdata, flags, reason_code,
                       properties):
        LOG.info("MQTT disconnected: %s", reason_code)
        self._marshal(self.signal_connected.emit, self, False)
