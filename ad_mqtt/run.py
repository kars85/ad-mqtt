import asyncio
import json
import logging
import pathlib
import time
from logging.handlers import RotatingFileHandler

import alarmdecoder as AD

from . import Devices
from .Bridge import Bridge
from .Client import Client
from .Discovery import Discovery
from .Mqtt import Mqtt


def setup_logging(log_cfg):
    fmt = '%(asctime)s.%(msecs)03d %(levelname)s %(module)s: %(message)s'
    datefmt = '%Y-%m-%d %H:%M:%S'
    formatter = logging.Formatter(fmt, datefmt)

    if log_cfg.screen:
        screen_handler = logging.StreamHandler()
        screen_handler.setFormatter(formatter)

    if log_cfg.file:
        file_handler = RotatingFileHandler(
            log_cfg.file, maxBytes=log_cfg.size_kb * 1000,
            backupCount=log_cfg.backup_count)
        file_handler.setFormatter(formatter)

    for name in log_cfg.modules:
        log = logging.getLogger(name)
        log.setLevel(log_cfg.level)
        if log_cfg.screen:
            log.addHandler(screen_handler)
        if log_cfg.file:
            log.addHandler(file_handler)


async def supervise(client):
    """Own the AD2 client lifecycle: readiness dispatch and reconnect.

    Direct heir of the legacy poll manager.  The client keeps its raw
    non-blocking socket; asyncio only provides read/write readiness.
    """
    loop = asyncio.get_running_loop()
    closed = asyncio.Event()
    fd = None

    def needs_write(link, active):
        if fd is None:
            return
        if active:
            loop.add_writer(fd, lambda: link.write_to_link(time.time()))
        else:
            loop.remove_writer(fd)

    def closing(link):
        # close() emits before the socket closes, so fd is still valid.
        nonlocal fd
        if fd is not None:
            loop.remove_reader(fd)
            loop.remove_writer(fd)
            fd = None
        closed.set()

    client.signal_needs_write.connect(needs_write)
    client.signal_closing.connect(closing)

    while True:
        if client.connect():
            closed.clear()
            fd = client.fileno()
            loop.add_reader(fd, client.read_from_link)
            # Re-emit write interest for bytes queued during connect (the
            # startup C/V queries land before the fd is registered).
            client.poll(time.time())
            await closed.wait()
        await asyncio.sleep(client.reconnect_dt)


HEARTBEAT_TOPIC = "alarm/bridge/heartbeat"
IN_FLIGHT_ALERT_S = 60


async def heartbeat(mqtt_client, bridge, path=None, interval=30):
    """Liveness and in-flight-age monitoring (handoff G14).

    Publishes a payload-free heartbeat and touches the optional heartbeat
    file (Docker HEALTHCHECK).  Crossing the in-flight age threshold only
    alerts; it never clears, retries, or unlocks an operation.
    """
    log = logging.getLogger(__name__)
    in_flight_since = None
    while True:
        monitor = bridge.command_monitor()
        if monitor["in_flight"]:
            if in_flight_since is None:
                in_flight_since = time.time()
            age = time.time() - in_flight_since
        else:
            in_flight_since = None
            age = 0

        if age > IN_FLIGHT_ALERT_S:
            log.warning(
                "Panel command in flight for %.0fs with %d AD2 "
                "acknowledgements outstanding; operator attention required",
                age,
                monitor["ack_remaining"],
            )

        payload = json.dumps({
            "time": time.time(),
            "in_flight_age": round(age, 1),
            "ack_remaining": monitor["ack_remaining"],
        })
        mqtt_client.publish(HEARTBEAT_TOPIC, payload, qos=0, retain=False)
        if path:
            pathlib.Path(path).touch()
        await asyncio.sleep(interval)


async def _run(cfg, alarm_code, devices):
    loop = asyncio.get_running_loop()
    zones, rf_devices = Devices.init_devices(devices)
    commands_enabled = getattr(cfg.alarm, "commands_enabled", False)

    # Alarm decoder network device.
    ad_client = Client(
        cfg.alarm.host,
        cfg.alarm.port,
        commands_enabled=commands_enabled,
    )
    decoder = AD.AlarmDecoder(ad_client)
    decoder.wire_events()

    mqtt_client = Mqtt(
        broker=cfg.mqtt.broker,
        port=cfg.mqtt.port,
        username=cfg.mqtt.username,
        password=cfg.mqtt.password,
        ca_cert=cfg.mqtt.encryption.ca_cert,
        certfile=cfg.mqtt.encryption.certfile,
        keyfile=cfg.mqtt.encryption.keyfile,
        availability_topic=cfg.mqtt.availability_topic,
        loop=loop,
    )

    bridge = Bridge(
        mqtt_client,
        decoder,
        alarm_code,
        zones,
        rf_devices,
        commands_enabled=commands_enabled,
        authorize_panel_write=ad_client.authorize_write,
        cancel_panel_write=ad_client.cancel_write,
    )
    Discovery(mqtt_client, bridge, zones)

    mqtt_client.start()
    try:
        if cfg.alarm.restore_on_startup:
            bridge.reset_all_zones()

        await asyncio.gather(
            supervise(ad_client),
            heartbeat(
                mqtt_client,
                bridge,
                path=getattr(cfg.alarm, "heartbeat_file", None),
            ),
        )
    finally:
        mqtt_client.stop()


def run(cfg, alarm_code, devices):
    setup_logging(cfg.log)

    log = logging.getLogger(__name__)

    try:
        asyncio.run(_run(cfg, alarm_code, devices))
    except Exception:
        log.exception("Unexpected exception")
        raise
