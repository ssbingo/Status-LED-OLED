"""Tests fuer den TOML-Konfigurationsloader: Mapping, Typumwandlung, unbekannte Schluessel."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import status_led as sl  # noqa: E402


class TestApplyConfigDict(unittest.TestCase):
    def test_maps_sections_to_fields(self):
        cfg = sl.Config()
        data = {
            "led_type": "ws2812-spi",
            "led": {"ws_count": 3, "ws_brightness": 0.8},
            "temp": {"threshold_c": 65.0},
            "network": {"check_host": "192.168.0.1"},
        }
        unknown = sl.apply_config_dict(cfg, data)
        self.assertEqual(unknown, [])
        self.assertEqual(cfg.led_type, "ws2812-spi")
        self.assertEqual(cfg.ws_count, 3)
        self.assertEqual(cfg.ws_brightness, 0.8)
        self.assertEqual(cfg.temp_threshold_c, 65.0)
        self.assertEqual(cfg.net_check_host, "192.168.0.1")

    def test_int_coerced_to_float_field(self):
        cfg = sl.Config()
        sl.apply_config_dict(cfg, {"temp": {"threshold_c": 70}})  # int im TOML
        self.assertIsInstance(cfg.temp_threshold_c, float)
        self.assertEqual(cfg.temp_threshold_c, 70.0)

    def test_list_coerced_to_tuple(self):
        cfg = sl.Config()
        sl.apply_config_dict(cfg, {"disk": {"devices": ["sda", "sdb"]}})
        self.assertEqual(cfg.disk_devices, ("sda", "sdb"))

    def test_hex_addr(self):
        cfg = sl.Config()
        sl.apply_config_dict(cfg, {"oled": {"addr": 0x3D}})
        self.assertEqual(cfg.oled_addr, 0x3D)

    def test_bool_field(self):
        cfg = sl.Config()
        sl.apply_config_dict(cfg, {"smart": {"enabled": True}, "oled": {"enabled": False}})
        self.assertTrue(cfg.smart_enabled)
        self.assertFalse(cfg.oled_enabled)

    def test_unknown_keys_reported(self):
        cfg = sl.Config()
        unknown = sl.apply_config_dict(cfg, {"led": {"bogus": 1}, "nope": 2})
        self.assertIn("led.bogus", unknown)
        self.assertIn("nope", unknown)

    def test_defaults_untouched_when_empty(self):
        cfg = sl.Config()
        ref = sl.Config()
        sl.apply_config_dict(cfg, {})
        self.assertEqual(cfg.led_type, ref.led_type)
        self.assertEqual(cfg.ws_count, ref.ws_count)


@unittest.skipIf(sl.tomllib is None, "tomllib erst ab Python 3.11")
class TestLoadConfigFile(unittest.TestCase):
    def test_load_from_toml_string(self):
        import tempfile
        toml = b'led_type = "console"\n[fan]\nenabled = true\nwarn_below_rpm = 600\n'
        with tempfile.NamedTemporaryFile("wb", suffix=".toml", delete=False) as f:
            f.write(toml)
            path = f.name
        try:
            cfg = sl.load_config(path)
            self.assertEqual(cfg.led_type, "console")
            self.assertTrue(cfg.fan_enabled)
            self.assertEqual(cfg.fan_warn_below_rpm, 600)
        finally:
            os.unlink(path)

    def test_missing_file_returns_defaults(self):
        cfg = sl.load_config("/nonexistent/path/config.toml")
        self.assertEqual(cfg.led_type, sl.Config().led_type)


if __name__ == "__main__":
    unittest.main()
