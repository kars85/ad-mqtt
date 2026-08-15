#!./venv/bin/python

import os
import sys

sys.path.insert(0, ".")
import ad_mqtt as AD  # noqa: E402

cfg = AD.Config()


def env_bool(name, default="false"):
    return os.getenv(name, default).strip().lower() in (
        "1", "true", "yes", "on"
    )


# Alarm Decoder ser2sock server location.
cfg.alarm.host = os.getenv("ADMQTT_SOCKET_HOST", "127.0.0.1")
cfg.alarm.port = int(os.getenv("ADMQTT_SOCKET_PORT", 10000))
# To reset all zones to closed (not faulted) on startup, set this to True
cfg.alarm.restore_on_startup = env_bool("ADMQTT_RESTORE_ON_STARTUP")
# Physical panel writes are disabled unless explicitly enabled after canary.
cfg.alarm.commands_enabled = env_bool("ADMQTT_COMMANDS_ENABLED")
cfg.alarm.command_unlock_file = os.getenv("ADMQTT_COMMANDS_UNLOCK_FILE")
# Liveness heartbeat file for container health checks (handoff G14).
cfg.alarm.heartbeat_file = os.getenv("ADMQTT_HEARTBEAT_FILE")

# MQTT Broker connection
cfg.mqtt.broker = os.getenv("ADMQTT_MQTT_HOST", "127.0.0.1")
cfg.mqtt.port = int(os.getenv("ADMQTT_MQTT_PORT", 1883))
# Optional user/pass for the broker
cfg.mqtt.username = os.getenv("ADMQTT_MQTT_USERNAME", None)
cfg.mqtt.password = os.getenv("ADMQTT_MQTT_PASSWORD", None)
# Optional encryption settings for the broker.
cfg.mqtt.encryption.ca_cert = os.getenv("ADMQTT_MQTT_CA_CERT", None)
cfg.mqtt.encryption.certfile = os.getenv("ADMQTT_MQTT_CERTFILE", None)
cfg.mqtt.encryption.keyfile = os.getenv("ADMQTT_MQTT_KEYFILE", None)

# Debugging information
cfg.log.level = os.getenv("ADMQTT_LOG_LEVEL", "INFO")
cfg.log.screen = env_bool("ADMQTT_LOG_SCREEN")
file_config = os.getenv("ADMQTT_LOG_FILE", "log.txt")
# Allowing env var to disable default file logging
if len(file_config) == 0:
    file_config = None
cfg.log.file = file_config
cfg.log.size_kb = 5000
cfg.log.backup_count = 3
cfg.log.modules = ["ad_mqtt"]

# Alarm code interface (handoff G15): the code comes from a restricted
# file.  An environment code is allowed only for an explicitly labeled
# exploratory hardware-characterization run, never a qualifying artifact.
alarm_code_file = os.getenv("ADMQTT_ALARM_CODE_FILE")
env_alarm_code = os.getenv("ADMQTT_ALARM_CODE")
alarm_code = None
if alarm_code_file:
    if env_alarm_code:
        raise RuntimeError(
            "Remove ADMQTT_ALARM_CODE from the environment when "
            "ADMQTT_ALARM_CODE_FILE is configured"
        )
    alarm_code = AD.Config.read_alarm_code(alarm_code_file)
elif env_alarm_code:
    if cfg.alarm.commands_enabled and not env_bool(
            "ADMQTT_ALARM_CODE_EXPLORATORY"):
        raise RuntimeError(
            "ADMQTT_ALARM_CODE_FILE is required when panel commands are "
            "enabled; ADMQTT_ALARM_CODE is allowed only with "
            "ADMQTT_ALARM_CODE_EXPLORATORY=1 for an exploratory "
            "hardware-characterization run"
        )
    alarm_code = env_alarm_code
if cfg.alarm.commands_enabled and not alarm_code:
    raise RuntimeError(
        "ADMQTT_ALARM_CODE is required when panel commands are enabled"
    )
if cfg.alarm.commands_enabled:
    cfg.consume_command_unlock(cfg.alarm.command_unlock_file)
if alarm_code is None:
    alarm_code = ""

# Zone configuration: YAML file, with a one-release legacy fallback to
# exec'ing devices.py.
devices_file = os.getenv("ADMQTT_DEVICES_FILE", "zones.yaml")
if os.path.exists(devices_file):
    devices = AD.Devices.load_devices(devices_file)
else:
    import warnings
    warnings.warn(
        "Loading zones by executing devices.py is deprecated and will be "
        "removed; convert to zones.yaml (see zones.yaml.example) and set "
        "ADMQTT_DEVICES_FILE if needed",
        DeprecationWarning,
    )
    exec(open("devices.py").read())
    devices = get_devices()  # noqa: F821

AD.run.run(cfg, alarm_code, devices)
