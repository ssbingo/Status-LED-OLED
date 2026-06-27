"""Tests fuer die Statuslogik: Prioritaeten, Hysterese und Aktivierungs-Flags.

Die Sensoren sind abstrahiert, daher genuegt es, einen Context mit Messwerten
zu fuellen und current_status() / die Conditions zu pruefen - ohne Hardware.

Ausfuehren (aus dem Projektverzeichnis):  python3 -m unittest discover -s tests
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import status_led as sl  # noqa: E402


def ctx(**kw):
    cfg = kw.pop("cfg", None) or sl.Config()
    c = sl.Context(cfg=cfg)
    for key, val in kw.items():
        setattr(c, key, val)
    return c


class TestPriority(unittest.TestCase):
    def test_ok_is_fallback(self):
        self.assertEqual(sl.current_status(ctx()).name, "ok")

    def test_highest_priority_wins(self):
        # Uebertemperatur (100) schlaegt Netzwerk (80) und Backup-Fehler (70)
        c = ctx(temp_c=80.0, network_down=True, backup_state="failed")
        self.assertEqual(sl.current_status(c).name, "overtemp")

    def test_network_beats_backup_failed(self):
        c = ctx(network_down=True, backup_state="failed")
        self.assertEqual(sl.current_status(c).name, "network_down")

    def test_smart_beats_network(self):
        cfg = sl.Config(smart_enabled=True)
        c = ctx(cfg=cfg, smart_failed=True, network_down=True)
        self.assertEqual(sl.current_status(c).name, "smart_warn")

    def test_diskspace_below_backup_failed_above_running(self):
        cfg = sl.Config()
        c = ctx(cfg=cfg, disk_free_pct=5.0, backup_state="running")
        self.assertEqual(sl.current_status(c).name, "diskspace_low")

    def test_cpuload_lowest_of_warnings(self):
        cfg = sl.Config(cpuload_threshold=1.0)
        c = ctx(cfg=cfg, cpu_load=5.0, backup_state="running")
        # backup_running (40) schlaegt cpuload_high (30)
        self.assertEqual(sl.current_status(c).name, "backup_running")


class TestOvertempHysteresis(unittest.TestCase):
    def test_latch_on_and_off(self):
        cfg = sl.Config(temp_threshold_c=70.0, temp_hysteresis_c=3.0)
        c = ctx(cfg=cfg)
        c.temp_c = 69.0
        self.assertFalse(sl.is_overtemp(c))         # unter Schwelle
        c.temp_c = 70.0
        self.assertTrue(sl.is_overtemp(c))          # erreicht Schwelle -> latch
        c.temp_c = 68.0
        self.assertTrue(sl.is_overtemp(c))          # in der Hysterese -> bleibt
        c.temp_c = 67.0
        self.assertFalse(sl.is_overtemp(c))         # <= Schwelle - Hysterese -> aus


class TestCpuLoadHysteresis(unittest.TestCase):
    def test_auto_threshold_uses_core_count(self):
        cfg = sl.Config(cpuload_threshold=0.0)
        self.assertEqual(sl.cpuload_threshold(cfg), float(os.cpu_count() or 1))

    def test_latch_on_and_off(self):
        cfg = sl.Config(cpuload_threshold=4.0, cpuload_hysteresis=0.5)
        c = ctx(cfg=cfg)
        c.cpu_load = 3.9
        self.assertFalse(sl.is_cpuload_high(c))
        c.cpu_load = 4.0
        self.assertTrue(sl.is_cpuload_high(c))
        c.cpu_load = 3.6
        self.assertTrue(sl.is_cpuload_high(c))      # in der Hysterese
        c.cpu_load = 3.5
        self.assertFalse(sl.is_cpuload_high(c))     # <= 4.0 - 0.5

    def test_disabled_never_triggers(self):
        cfg = sl.Config(cpuload_enabled=False, cpuload_threshold=1.0)
        self.assertFalse(sl.is_cpuload_high(ctx(cfg=cfg, cpu_load=99.0)))


class TestEnableFlags(unittest.TestCase):
    def test_diskspace_boundary(self):
        cfg = sl.Config(diskspace_min_free_percent=10.0)
        self.assertTrue(sl.is_diskspace_low(ctx(cfg=cfg, disk_free_pct=9.99)))
        self.assertFalse(sl.is_diskspace_low(ctx(cfg=cfg, disk_free_pct=10.0)))

    def test_diskspace_disabled(self):
        cfg = sl.Config(diskspace_enabled=False)
        self.assertFalse(sl.is_diskspace_low(ctx(cfg=cfg, disk_free_pct=0.0)))

    def test_smart_requires_enabled(self):
        self.assertFalse(sl.is_smart_failed(ctx(smart_failed=True)))  # default disabled
        cfg = sl.Config(smart_enabled=True)
        self.assertTrue(sl.is_smart_failed(ctx(cfg=cfg, smart_failed=True)))

    def test_fan_requires_enabled(self):
        self.assertFalse(sl.is_fan_warn(ctx(fan_failed=True)))        # default disabled
        cfg = sl.Config(fan_enabled=True)
        self.assertTrue(sl.is_fan_warn(ctx(cfg=cfg, fan_failed=True)))


class TestStatusText(unittest.TestCase):
    def test_every_status_has_text(self):
        for s in sl.STATUSES:
            self.assertIn(s.name, sl.STATUS_TEXT, f"kein STATUS_TEXT fuer {s.name}")


if __name__ == "__main__":
    unittest.main()
