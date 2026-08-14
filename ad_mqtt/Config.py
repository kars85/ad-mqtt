import logging
import os


class Data (dict):
    pass


class Config:
    @staticmethod
    def consume_command_unlock(path):
        """Atomically consume the explicit one-shot command unlock file."""
        if not path:
            raise RuntimeError(
                "ADMQTT_COMMANDS_UNLOCK_FILE is required when panel "
                "commands are enabled"
            )

        consumed_path = path + ".consumed"
        try:
            os.replace(path, consumed_path)
        except FileNotFoundError as error:
            raise RuntimeError(
                "Command unlock file is missing; inspect the panel before "
                "creating a new one"
            ) from error
        return consumed_path

    def __init__(self):
        # ser2sock server information
        self.alarm = Data()
        self.alarm.host = '127.0.0.1'
        self.alarm.port = 1000
        # Reset all zones to not faulted on startup
        self.alarm.restore_on_startup = False
        # Require an explicit opt-in before writing physical panel commands.
        self.alarm.commands_enabled = False
        self.alarm.command_unlock_file = None

        # MQTT broker (names must match insteon-mqtt settings variables)
        self.mqtt = Data()
        self.mqtt.broker = '127.0.0.1'
        self.mqtt.port = 1883
        self.mqtt.availability_topic = "alarm/available"
        # Optional user/pass for the broker
        self.mqtt.username = None
        self.mqtt.password = None
        self.mqtt.encryption = Data()
        # Optional encryption settings for the broker.
        self.mqtt.encryption.ca_cert = None
        self.mqtt.encryption.certfile = None
        self.mqtt.encryption.keyfile = None

        # Logging configuration
        self.log = Data()
        self.log.level = logging.INFO
        self.log.screen = True
        self.log.file = None
        self.log.size_kb = 5000
        self.log.backup_count = 3
        self.log.modules = ["ad_mqtt", "insteon_mqtt"]
