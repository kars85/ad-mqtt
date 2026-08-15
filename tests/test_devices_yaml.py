import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import ad_mqtt as AD  # noqa: E402
from ad_mqtt import Devices  # noqa: E402


class DevicesYamlTest(unittest.TestCase):
    def test_yaml_example_matches_legacy_devices_py(self):
        scope = {"AD": AD}
        exec((REPO_ROOT / "devices.py").read_text(), scope)
        legacy_zones, legacy_rf = Devices.init_devices(
            scope["get_devices"]())

        yaml_zones, yaml_rf = Devices.init_devices(
            Devices.load_devices(str(REPO_ROOT / "zones.yaml.example")))

        self.assertEqual(set(yaml_zones), set(legacy_zones))
        for num, legacy in legacy_zones.items():
            loaded = yaml_zones[num]
            self.assertEqual(loaded.entity, legacy.entity)
            self.assertEqual(loaded.label, legacy.label)
            self.assertEqual(loaded.device_class, legacy.device_class)
            self.assertEqual(loaded.has_battery, legacy.has_battery)

        self.assertEqual(set(yaml_rf), set(legacy_rf))
        for serial, legacy in legacy_rf.items():
            loaded_loops = [
                loop.zone if loop else None
                for loop in yaml_rf[serial].loops
            ]
            legacy_loops = [
                loop.zone if loop else None
                for loop in legacy.loops
            ]
            self.assertEqual(loaded_loops, legacy_loops)


if __name__ == "__main__":
    unittest.main()
