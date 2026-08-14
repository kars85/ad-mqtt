import logging
from logging.handlers import RotatingFileHandler

import alarmdecoder as AD
import insteon_mqtt as IM

from . import Devices
from .Bridge import Bridge
from .Client import Client
from .Discovery import Discovery


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


def run(cfg, alarm_code, devices, restore_all=False):
    setup_logging(cfg.log)

    log = logging.getLogger(__name__)

    try:
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

        mqtt_client = IM.network.Mqtt(id="ad-mqtt")
        # IM uses dict: cfg['a'] not cfg.a
        mqtt_client.load_config(cfg.mqtt.__dict__)

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

        loop = IM.network.poll.Manager()
        loop.add(ad_client, connected=False)
        loop.add(mqtt_client, connected=False)

        if cfg.alarm.restore_on_startup:
            bridge.reset_all_zones()

        while loop.active():
            loop.select()
    except Exception:
        log.exception("Unexpected exception")
        raise
