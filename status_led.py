#!/usr/bin/env python3
"""
status_led.py - RGB-Status-LED (WS2812B) + OLED-Display (SSD1306) fuer Raspberry Pi 4

Eine RGB-LED zeigt den Systemzustand per Farbe an, ein optionales 128x32-OLED
(I2C, SSD1306 / Adafruit PiOLED) zeigt parallel Klartext-Details (IP, CPU, RAM, Status).

LED-Zustaende nach Prioritaet (hoch -> niedrig):
  - Rot/Gruen im 1-Sek-Wechsel          = Uebertemperatur (Schwelle konfigurierbar)
  - Blau blinkend (2 Hz)                = kein Netzwerk
  - Magenta blinkend (2 Hz)             = Backup fehlgeschlagen
  - Cyan pulsierend                     = Backup laeuft
  - Gruen (dim=Leerlauf, hell=Disk-I/O) = Normalbetrieb + HDD-Aktivitaet

Busse: LED nutzt SPI (GPIO10) oder PWM (GPIO18), OLED nutzt I2C (GPIO2/3) -> kein Konflikt.

LED-Typen (Config.led_type):
  - "analog"      : klassische 5050-RGB-LED an 3 GPIOs, PWM ueber gpiozero
  - "ws2812"      : WS2812B ueber PWM (rpi_ws281x), GPIO18, benoetigt root
  - "ws2812-spi"  : WS2812B ueber SPI (MOSI/GPIO10), KEIN root, kein Audio-Konflikt
  - "console"     : Ausgabe im Terminal (zum Testen ohne Hardware)

Ohne Hardware testen:   python3 status_led.py --simulate --duration 30
Echtbetrieb:            python3 status_led.py
"""

from __future__ import annotations

import argparse
import glob
import json
import math
import os
import random
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

try:
    import tomllib                    # Python 3.11+ (Raspberry Pi OS Bookworm)
except ModuleNotFoundError:           # pragma: no cover - aeltere Python-Versionen
    tomllib = None

__version__ = "1.3.1"

# ============================================================================
# Konfiguration  --  hier alles Wichtige einstellen
# ============================================================================


@dataclass
class Config:
    # --- LED-Typ ---
    led_type: str = "ws2812"          # "ws2812" (PWM, empfohlen) | "ws2812-spi" | "analog" | "console"

    # --- Verkabelung der analogen RGB-LED (BCM-GPIO-Nummern) ---
    pin_red: int = 17
    pin_green: int = 27
    pin_blue: int = 22
    active_high: bool = True          # gem. Kathode -> True, gem. Anode -> False

    # --- WS2812B / NeoPixel ---
    ws_pin: int = 18                  # Datenpin fuer "ws2812" (PWM): GPIO18.
                                      # Bei "ws2812-spi" fest auf MOSI/GPIO10 (Wert wird ignoriert).
    ws_count: int = 1                 # Anzahl LEDs
    ws_brightness: float = 0.50       # Master-Helligkeit 0..1 (WS2812B sind sehr hell)
    ws_pixel_order: str = "GRB"       # Farbreihenfolge: "GRB" (Standard WS2812B) oder "RGB"

    # --- OLED-Display (SSD1306, I2C) ---
    oled_enabled: bool = True         # OLED parallel zur LED ansteuern
    oled_width: int = 128
    oled_height: int = 32
    oled_addr: int = 0x3C             # Standardadresse der PiOLED (i2cdetect -y 1)
    oled_poll_s: float = 1.0          # OLED nur 1x/Sekunde neu zeichnen (I2C entlasten)
    oled_page_timeout_s: float = 30.0 # Auto-Ruecksprung auf Seite 0 nach Inaktivitaet

    # --- Taster (kurz: Display weiterschalten / lang: Neustart) ---
    button_enabled: bool = True
    button_pin: int = 17              # BCM; Taster gegen GND, interner Pull-up (gedrueckt = LOW)
    button_long_press_s: float = 5.0  # ab dieser Haltedauer -> Reboot
    button_debounce_s: float = 0.05   # Mindest-Druckdauer fuer "kurz"
    button_reboot_message_s: float = 3.0  # so lange "Neustart" anzeigen, bevor der Reboot ausgeloest wird

    # --- Temperatur ---
    temp_threshold_c: float = 70.0    # ab hier "Uebertemperatur"
    temp_hysteresis_c: float = 3.0    # Rueckfall erst bei (Schwelle - Hysterese)
    temp_path: str = "/sys/class/thermal/thermal_zone0/temp"
    temp_poll_s: float = 2.0

    # --- HDD-/Disk-Aktivitaet (ueber /proc/diskstats) ---
    disk_poll_s: float = 0.05
    disk_active_hold_s: float = 0.12
    disk_devices: tuple[str, ...] = ()  # leer = automatisch (alle ausser loop*/ram*)

    # --- Netzwerk-Status ---
    net_check_host: str = "1.1.1.1"   # Erreichbarkeitstest; fuer LAN die Gateway-IP eintragen
    net_check_port: int = 53
    net_check_timeout: float = 1.0
    net_poll_s: float = 5.0
    net_iface: str = ""               # leer = alle Interfaces ausser lo summieren (Durchsatz-Anzeige)
    net_throughput_enabled: bool = True

    # --- Backup-Status (wird aus einer Status-Datei gelesen) ---
    backup_status_path: str = "/run/status-led/backup"
    backup_poll_s: float = 1.0

    # --- CPU-Last (hohe Auslastung) ---
    cpuload_enabled: bool = True
    cpuload_threshold: float = 0.0    # 1-Minuten-Load-Schwelle; 0 = automatisch (= Anzahl CPU-Kerne)
    cpuload_hysteresis: float = 0.5   # Rueckfall erst bei (Schwelle - Hysterese)
    cpuload_poll_s: float = 5.0

    # --- Freier Speicherplatz ---
    diskspace_enabled: bool = True
    diskspace_path: str = "/"
    diskspace_min_free_percent: float = 10.0  # Warnung, wenn weniger frei
    diskspace_poll_s: float = 30.0

    # --- SMART-Festplattengesundheit (benoetigt smartmontools + root) ---
    smart_enabled: bool = False
    smart_devices: tuple[str, ...] = ()  # leer = automatisch (smartctl --scan)
    smart_poll_s: float = 300.0

    # --- Luefter (hwmon-Tacho ODER thermal cooling_device, z. B. PoE-HAT) ---
    fan_enabled: bool = False
    fan_warn_below_rpm: int = 0       # 0 = nie warnen (nur Drehzahl anzeigen); sonst Warnung unter dieser Drehzahl
    fan_warn_at_max: bool = False     # bei stufengesteuerten Lueftern (PoE-HAT): warnen, wenn Dauer-Maximalstufe
    fan_poll_s: float = 5.0
    fan_hwmon_glob: str = ""          # optionaler fester Pfad zu fanX_input; leer = automatisch suchen

    # --- Helligkeiten (0..1) ---
    green_idle: float = 0.25
    green_active: float = 1.00
    red_level: float = 1.00

    # --- Hauptschleife ---
    tick_s: float = 0.05
    duration_s: float = 0.0           # 0 = endlos (sonst Laufzeitbegrenzung, v.a. zum Testen)


RGB = tuple[float, float, float]      # je 0..1
OFF: RGB = (0.0, 0.0, 0.0)


# ============================================================================
# Konfigurationsdatei (TOML)  --  optional, ueberschreibt die Defaults oben
# ============================================================================
#
# Liegt unter /etc/status-led/config.toml (per --config aenderbar). Sektionen
# bilden Gruppen, die Schluessel werden auf die Felder von Config gemappt.
# Fehlt die Datei, gelten die Standardwerte. Siehe config.example.toml.

DEFAULT_CONFIG_PATH = "/etc/status-led/config.toml"

# dotted TOML-Schluessel -> Config-Feldname
CONFIG_MAP: dict[str, str] = {
    "led_type": "led_type",
    "led.pin_red": "pin_red",
    "led.pin_green": "pin_green",
    "led.pin_blue": "pin_blue",
    "led.active_high": "active_high",
    "led.ws_pin": "ws_pin",
    "led.ws_count": "ws_count",
    "led.ws_brightness": "ws_brightness",
    "led.ws_pixel_order": "ws_pixel_order",
    "oled.enabled": "oled_enabled",
    "oled.width": "oled_width",
    "oled.height": "oled_height",
    "oled.addr": "oled_addr",
    "oled.poll_s": "oled_poll_s",
    "oled.page_timeout_s": "oled_page_timeout_s",
    "button.enabled": "button_enabled",
    "button.pin": "button_pin",
    "button.long_press_s": "button_long_press_s",
    "button.debounce_s": "button_debounce_s",
    "button.reboot_message_s": "button_reboot_message_s",
    "temp.threshold_c": "temp_threshold_c",
    "temp.hysteresis_c": "temp_hysteresis_c",
    "temp.path": "temp_path",
    "temp.poll_s": "temp_poll_s",
    "disk.poll_s": "disk_poll_s",
    "disk.active_hold_s": "disk_active_hold_s",
    "disk.devices": "disk_devices",
    "network.check_host": "net_check_host",
    "network.check_port": "net_check_port",
    "network.check_timeout": "net_check_timeout",
    "network.poll_s": "net_poll_s",
    "network.iface": "net_iface",
    "network.throughput_enabled": "net_throughput_enabled",
    "backup.status_path": "backup_status_path",
    "backup.poll_s": "backup_poll_s",
    "cpuload.enabled": "cpuload_enabled",
    "cpuload.threshold": "cpuload_threshold",
    "cpuload.hysteresis": "cpuload_hysteresis",
    "cpuload.poll_s": "cpuload_poll_s",
    "diskspace.enabled": "diskspace_enabled",
    "diskspace.path": "diskspace_path",
    "diskspace.min_free_percent": "diskspace_min_free_percent",
    "diskspace.poll_s": "diskspace_poll_s",
    "smart.enabled": "smart_enabled",
    "smart.devices": "smart_devices",
    "smart.poll_s": "smart_poll_s",
    "fan.enabled": "fan_enabled",
    "fan.warn_below_rpm": "fan_warn_below_rpm",
    "fan.warn_at_max": "fan_warn_at_max",
    "fan.poll_s": "fan_poll_s",
    "fan.hwmon": "fan_hwmon_glob",
    "brightness.green_idle": "green_idle",
    "brightness.green_active": "green_active",
    "brightness.red_level": "red_level",
    "loop.tick_s": "tick_s",
    "loop.duration_s": "duration_s",
}


def _flatten_toml(data: dict, prefix: str = "") -> dict:
    """Verschachtelte TOML-Tabellen zu dotted keys verflachen (eine Ebene tief)."""
    out: dict = {}
    for key, val in data.items():
        dotted = f"{prefix}{key}"
        if isinstance(val, dict):
            out.update(_flatten_toml(val, dotted + "."))
        else:
            out[dotted] = val
    return out


def _coerce(current, value):
    """Wert auf den Typ des bestehenden Config-Defaults bringen."""
    if isinstance(current, bool):          # vor int pruefen (bool ist int-Subklasse)
        return bool(value)
    if isinstance(current, tuple):
        return tuple(str(x) for x in value)
    if isinstance(current, int):
        return int(value)
    if isinstance(current, float):
        return float(value)
    if isinstance(current, str):
        return str(value)
    return value


def apply_config_dict(cfg: "Config", data: dict) -> list[str]:
    """Wendet ein (bereits geparstes) TOML-Dict auf cfg an.
    Liefert die Liste unbekannter Schluessel (fuer Warnungen/Tests)."""
    unknown: list[str] = []
    for dotted, value in _flatten_toml(data).items():
        field = CONFIG_MAP.get(dotted)
        if field is None:
            unknown.append(dotted)
            continue
        try:
            setattr(cfg, field, _coerce(getattr(cfg, field), value))
        except (TypeError, ValueError):
            print(f"Konfig-Wert ungueltig fuer '{dotted}': {value!r}", file=sys.stderr)
    return unknown


def load_config(path: str, required: bool = False) -> Config:
    """Liest die TOML-Konfiguration. Fehlt sie, gelten die Defaults aus Config."""
    cfg = Config()
    p = Path(path)
    if not p.exists():
        if required:
            print(f"Konfig nicht gefunden: {path} - nutze Standardwerte", file=sys.stderr)
        return cfg
    if tomllib is None:
        print("Konfig ignoriert: Python < 3.11 hat kein tomllib - nutze Standardwerte", file=sys.stderr)
        return cfg
    try:
        with open(p, "rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as e:
        print(f"Konfig-Fehler ({e}) - nutze Standardwerte", file=sys.stderr)
        return cfg
    for key in apply_config_dict(cfg, data):
        print(f"Unbekannter Konfig-Schluessel ignoriert: {key}", file=sys.stderr)
    return cfg


# ============================================================================
# LED-Treiber  --  Hardware-Abstraktion
# ============================================================================


class LedDriver(ABC):
    @abstractmethod
    def set_color(self, color: RGB) -> None: ...

    def close(self) -> None:
        pass


class AnalogRgbLed(LedDriver):
    """Klassische 5050-RGB-LED an drei GPIOs (PWM via gpiozero)."""

    def __init__(self, cfg: Config):
        from gpiozero import RGBLED
        self._led = RGBLED(red=cfg.pin_red, green=cfg.pin_green, blue=cfg.pin_blue,
                           active_high=cfg.active_high, pwm=True)

    def set_color(self, color: RGB) -> None:
        self._led.color = color

    def close(self) -> None:
        self._led.close()


class NeoPixelLed(LedDriver):
    """WS2812B ueber PWM (rpi_ws281x via Blinka). Benoetigt root + GPIO18."""

    def __init__(self, cfg: Config):
        import board
        import neopixel
        pin = getattr(board, f"D{cfg.ws_pin}")
        self._px = neopixel.NeoPixel(pin, cfg.ws_count, brightness=cfg.ws_brightness,
                                     auto_write=True, pixel_order=cfg.ws_pixel_order)

    def set_color(self, color: RGB) -> None:
        r, g, b = (int(round(c * 255)) for c in color)
        self._px.fill((r, g, b))

    def close(self) -> None:
        self._px.fill((0, 0, 0))
        self._px.deinit()


class NeoPixelSpiLed(LedDriver):
    """WS2812B ueber SPI (Datenleitung an MOSI/GPIO10). KEIN root, kein Audio-Konflikt.
    Voraussetzung: SPI aktiviert (raspi-config bzw. dtparam=spi=on)."""

    def __init__(self, cfg: Config):
        import board
        import neopixel_spi as neopixel
        spi = board.SPI()
        self._px = neopixel.NeoPixel_SPI(spi, cfg.ws_count, brightness=cfg.ws_brightness,
                                         auto_write=True, pixel_order=cfg.ws_pixel_order)

    def set_color(self, color: RGB) -> None:
        r, g, b = (int(round(c * 255)) for c in color)
        self._px.fill((r, g, b))

    def close(self) -> None:
        self._px.fill((0, 0, 0))


class ConsoleLed(LedDriver):
    """Gibt die Farbe im Terminal aus. Im TTY als Live-Block, sonst nur bei Aenderung."""

    def __init__(self, cfg: Config | None = None):
        self._tty = sys.stdout.isatty()
        self._last: tuple[int, int, int] | None = None

    def set_color(self, color: RGB) -> None:
        r, g, b = (int(round(c * 255)) for c in color)
        if self._tty:
            sys.stdout.write(f"\r\x1b[48;2;{r};{g};{b}m   \x1b[0m rgb({r:3d},{g:3d},{b:3d})  ")
            sys.stdout.flush()
        else:
            key = (r, g, b)
            if key != self._last:
                self._last = key
                ts = time.strftime("%H:%M:%S")
                print(f"{ts}  LED -> rgb({r:3d},{g:3d},{b:3d})  {self._label(r, g, b)}")

    @staticmethod
    def _label(r: int, g: int, b: int) -> str:
        if r > 0 and g == 0 and b == 0:
            return "ROT (Uebertemperatur)"
        if r == 0 and g > 0 and b == 0:
            return "GRUEN hell (Disk-I/O)" if g >= 200 else "gruen (Normalbetrieb)"
        if r == 0 and g == 0 and b > 0:
            return "BLAU (kein Netzwerk)"
        if r > 0 and g == 0 and b > 0:
            return "MAGENTA (Backup fehlgeschlagen)"
        if r == 0 and g > 0 and b > 0:
            return "CYAN (Backup laeuft)"
        if r > 0 and g > 0 and b > 0:
            return "WEISS (SMART-Fehler)"
        if r > 0 and g > 0 and b == 0:      # gelb/orange/bernstein
            if g >= r:
                return "GELB (Speicher voll)"
            return "ORANGE/BERNSTEIN (Luefter / CPU-Last)"
        if (r, g, b) == (0, 0, 0):
            return "aus"
        return ""

    def close(self) -> None:
        if self._tty:
            sys.stdout.write("\r" + " " * 40 + "\r")
            sys.stdout.flush()


# ============================================================================
# Sensoren
# ============================================================================


class TemperatureSensor:
    """CPU-Temperatur aus dem sysfs-Thermalzonen-File (millidegree C)."""

    def __init__(self, cfg: Config):
        self._path = Path(cfg.temp_path)
        self._poll = cfg.temp_poll_s
        self._last_read = 0.0
        self._value = 0.0

    def read(self, now: float) -> float:
        if now - self._last_read >= self._poll:
            self._last_read = now
            try:
                self._value = int(self._path.read_text().strip()) / 1000.0
            except (OSError, ValueError):
                pass
        return self._value


class DiskActivity:
    """Lese-/Schreibaktivitaet ueber Aenderungen in /proc/diskstats."""

    def __init__(self, cfg: Config):
        self._poll = cfg.disk_poll_s
        self._hold = cfg.disk_active_hold_s
        self._devices = set(cfg.disk_devices)
        self._last_poll = 0.0
        self._last_total = self._read_total()
        self._active_until = 0.0

    def _read_total(self) -> int:
        total = 0
        try:
            with open("/proc/diskstats") as f:
                for line in f:
                    parts = line.split()
                    if len(parts) < 14:
                        continue
                    name = parts[2]
                    if self._devices:
                        if name not in self._devices:
                            continue
                    elif name.startswith(("loop", "ram")):
                        continue
                    total += int(parts[5]) + int(parts[9])  # gelesene + geschriebene Sektoren
        except OSError:
            pass
        return total

    def is_active(self, now: float) -> bool:
        if now - self._last_poll >= self._poll:
            self._last_poll = now
            total = self._read_total()
            if total != self._last_total:
                self._active_until = now + self._hold
            self._last_total = total
        return now < self._active_until


class BackupStatus:
    """Liest 'running' / 'ok' / 'failed' aus einer Status-Datei (vom Backup-Job geschrieben)."""

    def __init__(self, cfg: Config):
        self._path = Path(cfg.backup_status_path)
        self._poll = cfg.backup_poll_s
        self._last = 0.0
        self._state = "ok"

    def read(self, now: float) -> str:
        if now - self._last >= self._poll:
            self._last = now
            try:
                txt = self._path.read_text().strip().lower()
            except OSError:
                txt = ""
            if txt == "running":
                self._state = "running"
            elif txt in ("failed", "fail", "error"):
                self._state = "failed"
            else:
                self._state = "ok"
        return self._state


def check_internet(host: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


class BackgroundValue:
    """Ruft fn() periodisch im Daemon-Thread auf und haelt das letzte Ergebnis.
    Fuer potenziell blockierende Pruefungen (z.B. Netzwerk)."""

    def __init__(self, fn: Callable[[], object], interval_s: float, initial: object = None):
        self._fn = fn
        self._interval = interval_s
        self._value = initial
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                v = self._fn()
            except Exception:
                v = self._value
            with self._lock:
                self._value = v
            self._stop.wait(self._interval)

    @property
    def value(self):
        with self._lock:
            return self._value

    def stop(self) -> None:
        self._stop.set()


class CpuLoadSensor:
    """1-Minuten-Load ueber os.getloadavg()."""

    def __init__(self, cfg: Config):
        self._poll = cfg.cpuload_poll_s
        self._last = 0.0
        self._value = 0.0

    def read(self, now: float) -> float:
        if now - self._last >= self._poll:
            self._last = now
            try:
                self._value = os.getloadavg()[0]
            except OSError:
                pass
        return self._value


class DiskSpaceSensor:
    """Freier Speicherplatz in Prozent (shutil.disk_usage)."""

    def __init__(self, cfg: Config):
        self._path = cfg.diskspace_path
        self._poll = cfg.diskspace_poll_s
        self._last = 0.0
        self._value = 100.0

    def read(self, now: float) -> float:
        if now - self._last >= self._poll:
            self._last = now
            try:
                u = shutil.disk_usage(self._path)
                self._value = u.free / u.total * 100.0 if u.total else 100.0
            except OSError:
                pass
        return self._value


def find_fan_rpm_path(override: str = "") -> str | None:
    """hwmon-Tacho mit RPM (fanX_input), sofern vorhanden."""
    candidates = (
        "/sys/class/hwmon/hwmon*/fan*_input",
        "/sys/devices/platform/cooling_fan/hwmon/hwmon*/fan*_input",
    )
    patterns = (override,) if override else candidates
    for pat in patterns:
        matches = sorted(glob.glob(pat))
        if matches:
            return matches[0]
    return None


def find_fan_cooling_dev() -> str | None:
    """thermal cooling_device eines Luefters (z. B. PoE-HAT: 'rpi-poe-fan').
    Diese liefern keine RPM, sondern eine Stufe (cur_state/max_state)."""
    for dev in sorted(glob.glob("/sys/class/thermal/cooling_device*")):
        try:
            t = Path(dev + "/type").read_text().strip().lower()
        except OSError:
            continue
        if "fan" in t or "poe" in t:
            return dev
    return None


class FanSensor:
    """Luefterinfo: bevorzugt hwmon-Tacho (RPM); sonst thermal cooling_device
    (Stufe, z. B. offizielles PoE-/PoE+-HAT, dessen Luefter firmwaregesteuert ist)."""

    def __init__(self, cfg: Config):
        self._poll = cfg.fan_poll_s
        self._last = 0.0
        self.rpm: int | None = None
        self.level: int | None = None
        self.max_level: int | None = None
        self._rpm_path = find_fan_rpm_path(cfg.fan_hwmon_glob)
        self._cool_path = None if self._rpm_path else find_fan_cooling_dev()

    def read(self, now: float) -> None:
        if now - self._last < self._poll:
            return
        self._last = now
        if self._rpm_path:
            try:
                self.rpm = int(Path(self._rpm_path).read_text().strip())
            except (OSError, ValueError):
                self.rpm = None
        elif self._cool_path:
            try:
                self.level = int(Path(self._cool_path + "/cur_state").read_text().strip())
                self.max_level = int(Path(self._cool_path + "/max_state").read_text().strip())
            except (OSError, ValueError):
                self.level = self.max_level = None


def _smartctl_json(dev: str) -> dict:
    """smartctl-JSON fuer ein Geraet; probiert auch '-d sat' (USB-SATA-Bruecken)."""
    for extra in ([], ["-d", "sat"]):
        try:
            out = subprocess.run(["smartctl", "-H", "-A", "-j", *extra, dev],
                                 capture_output=True, text=True, timeout=20)
            data = json.loads(out.stdout or "{}")
        except (OSError, subprocess.SubprocessError, ValueError):
            continue
        if data.get("smart_status") is not None or data.get("temperature") is not None:
            return data
    return {}


def smart_scan() -> list[str]:
    """Geraete via 'smartctl --scan' ermitteln (inkl. '-d sat'-Variante fuer USB).
    Faellt auf vorhandene Block-Devices zurueck, falls der Scan leer bleibt."""
    devices: list[str] = []
    for args in (["smartctl", "--scan", "-j"], ["smartctl", "--scan", "-d", "sat", "-j"]):
        try:
            out = subprocess.run(args, capture_output=True, text=True, timeout=10)
            data = json.loads(out.stdout or "{}")
        except (OSError, subprocess.SubprocessError, ValueError):
            continue
        for d in data.get("devices", []):
            name = d.get("name")
            if name and name not in devices:
                devices.append(name)
    if not devices:
        for pat in ("/dev/sd[a-z]", "/dev/nvme[0-9]n[0-9]"):
            devices.extend(sorted(glob.glob(pat)))
    return devices


def smart_probe(cfg: Config) -> dict:
    """Fragt SMART-Status + Temperatur ab. Liefert {'failed': bool, 'temp_c': float|None}.
    Bei fehlendem smartctl o. Fehlern: failed=False (kein Fehlalarm)."""
    devices = list(cfg.smart_devices) or smart_scan()
    failed = False
    temp: float | None = None
    for dev in devices:
        data = _smartctl_json(dev)
        if data.get("smart_status", {}).get("passed") is False:
            failed = True
        t = data.get("temperature", {}).get("current")
        if isinstance(t, (int, float)):
            temp = t if temp is None else max(temp, t)
    return {"failed": failed, "temp_c": temp}


class NetThroughput:
    """Netzwerk-Durchsatz (Bytes/s) aus /sys/class/net/*/statistics."""

    def __init__(self, cfg: Config):
        self._iface = cfg.net_iface
        self._poll = 1.0
        self._last_t = 0.0
        self._rx_rate = 0.0
        self._tx_rate = 0.0
        self._prev = self._read_counters()

    def _read_counters(self) -> tuple[int, int]:
        if self._iface:
            bases = [f"/sys/class/net/{self._iface}"]
        else:
            bases = [p for p in glob.glob("/sys/class/net/*") if not p.endswith("/lo")]
        rx = tx = 0
        for base in bases:
            try:
                rx += int(Path(base + "/statistics/rx_bytes").read_text())
                tx += int(Path(base + "/statistics/tx_bytes").read_text())
            except (OSError, ValueError):
                pass
        return rx, tx

    def read(self, now: float) -> tuple[float, float]:
        if now - self._last_t >= self._poll:
            rx, tx = self._read_counters()
            if self._last_t:
                dt = now - self._last_t
                self._rx_rate = max(0, rx - self._prev[0]) / dt
                self._tx_rate = max(0, tx - self._prev[1]) / dt
            self._prev = (rx, tx)
            self._last_t = now
        return self._rx_rate, self._tx_rate


# --- Simulierte Sensoren (nur fuer --simulate) -------------------------------


class SimTemperature:
    def __init__(self, cfg: Config):
        self._start = time.monotonic()

    def read(self, now: float) -> float:
        return 65.0 + 18.0 * math.sin((now - self._start) * 2 * math.pi / 8.0)


class SimDisk:
    def __init__(self, cfg: Config):
        self._until = 0.0

    def is_active(self, now: float) -> bool:
        if now > self._until and random.random() < 0.05:
            self._until = now + random.uniform(0.05, 0.20)
        return now < self._until


class SimNetwork:
    def __init__(self, cfg: Config):
        self._start = time.monotonic()

    @property
    def value(self) -> bool:  # True = kein Netzwerk
        return (time.monotonic() - self._start) % 15.0 < 3.0

    def stop(self) -> None:
        pass


class SimBackup:
    def __init__(self, cfg: Config):
        self._start = time.monotonic()

    def read(self, now: float) -> str:
        t = (now - self._start) % 24.0
        if 5.0 <= t < 9.0:
            return "running"
        if 14.0 <= t < 17.0:
            return "failed"
        return "ok"


class SimCpuLoad:
    def __init__(self, cfg: Config):
        self._start = time.monotonic()
        self._th = cfg.cpuload_threshold or float(os.cpu_count() or 4)

    def read(self, now: float) -> float:
        # schwingt zwischen 0,5x und 1,5x Schwelle -> kreuzt die Grenze regelmaessig
        return self._th * (0.5 + (math.sin((now - self._start) * 2 * math.pi / 12.0) * 0.5 + 0.5))


class SimDiskSpace:
    def __init__(self, cfg: Config):
        self._start = time.monotonic()

    def read(self, now: float) -> float:
        # pendelt zwischen ~5% und ~25% frei -> faellt zeitweise unter 10%
        return 5.0 + 20.0 * (math.sin((now - self._start) * 2 * math.pi / 18.0) * 0.5 + 0.5)


class SimFan:
    def __init__(self, cfg: Config):
        self._start = time.monotonic()
        self.rpm: int | None = None
        self.level: int | None = None
        self.max_level: int | None = None

    def read(self, now: float) -> None:
        t = (now - self._start) % 20.0
        if t < 3.0:
            self.rpm = 0                   # Luefter steht (loest Warnung aus, wenn Schwelle gesetzt)
        else:
            self.rpm = int(1500 + 400 * math.sin(now))


class SimSmart:
    def __init__(self, cfg: Config):
        self._start = time.monotonic()

    @property
    def value(self) -> dict:
        t = (time.monotonic() - self._start) % 30.0
        return {"failed": 20.0 <= t < 24.0, "temp_c": 38.0 + 5.0 * math.sin(t)}

    def stop(self) -> None:
        pass


class SimNetThroughput:
    def __init__(self, cfg: Config):
        self._start = time.monotonic()

    def read(self, now: float) -> tuple[float, float]:
        base = now - self._start
        return abs(math.sin(base)) * 2_000_000, abs(math.cos(base)) * 500_000


# ============================================================================
# Zustand (Status)  --  Kern der Erweiterbarkeit
# ============================================================================


@dataclass
class Context:
    """Aktuelle Messwerte, jeden Tick aktualisiert."""
    cfg: Config
    now: float = 0.0
    temp_c: float = 0.0
    disk_active: bool = False
    network_down: bool = False
    backup_state: str = "ok"
    overtemp_latched: bool = False
    cpu_load: float = 0.0
    cpuload_latched: bool = False
    disk_free_pct: float = 100.0
    smart_failed: bool = False
    disk_temp_c: float | None = None
    fan_rpm: int | None = None
    fan_level: int | None = None
    fan_max_level: int | None = None
    fan_failed: bool = False
    net_rx_rate: float = 0.0
    net_tx_rate: float = 0.0


@dataclass
class StatusDef:
    name: str
    priority: int                       # hoeher = wichtiger
    condition: Callable[[Context], bool]
    render: Callable[[Context], RGB]


def is_overtemp(ctx: Context) -> bool:
    cfg = ctx.cfg
    if ctx.overtemp_latched:
        if ctx.temp_c <= cfg.temp_threshold_c - cfg.temp_hysteresis_c:
            ctx.overtemp_latched = False
    elif ctx.temp_c >= cfg.temp_threshold_c:
        ctx.overtemp_latched = True
    return ctx.overtemp_latched


def render_overtemp(ctx: Context) -> RGB:
    cfg = ctx.cfg
    if int(ctx.now) % 2 == 0:               # 1-Sekunden-Wechsel Rot <-> Gruen
        return (cfg.red_level, 0.0, 0.0)
    return (0.0, cfg.green_active, 0.0)


def is_network_down(ctx: Context) -> bool:
    return ctx.network_down


def render_network_down(ctx: Context) -> RGB:
    on = int(ctx.now * 2) % 2 == 0          # blau, 2 Hz
    return (0.0, 0.0, 1.0) if on else OFF


def is_backup_failed(ctx: Context) -> bool:
    return ctx.backup_state == "failed"


def render_backup_failed(ctx: Context) -> RGB:
    on = int(ctx.now * 2) % 2 == 0          # magenta, 2 Hz
    return (1.0, 0.0, 1.0) if on else OFF


def is_backup_running(ctx: Context) -> bool:
    return ctx.backup_state == "running"


def render_backup_running(ctx: Context) -> RGB:
    level = 0.15 + 0.85 * (math.sin(ctx.now * math.pi) * 0.5 + 0.5)  # cyan, langsamer Puls (~2 s)
    return (0.0, level, level)


def is_smart_failed(ctx: Context) -> bool:
    return ctx.cfg.smart_enabled and ctx.smart_failed


def render_smart_failed(ctx: Context) -> RGB:
    on = int(ctx.now * 3) % 2 == 0          # weiss, schnelles Blinken (3 Hz)
    return (1.0, 1.0, 1.0) if on else OFF


def is_fan_warn(ctx: Context) -> bool:
    return ctx.cfg.fan_enabled and ctx.fan_failed


def render_fan_warn(ctx: Context) -> RGB:
    on = int(ctx.now * 2) % 2 == 0          # orange, 2 Hz
    return (1.0, 0.35, 0.0) if on else OFF


def is_diskspace_low(ctx: Context) -> bool:
    cfg = ctx.cfg
    return cfg.diskspace_enabled and ctx.disk_free_pct < cfg.diskspace_min_free_percent


def render_diskspace_low(ctx: Context) -> RGB:
    on = int(ctx.now) % 2 == 0              # gelb, langsames Blinken (1 Hz)
    return (1.0, 1.0, 0.0) if on else OFF


def cpuload_threshold(cfg: Config) -> float:
    return cfg.cpuload_threshold if cfg.cpuload_threshold > 0 else float(os.cpu_count() or 1)


def is_cpuload_high(ctx: Context) -> bool:
    cfg = ctx.cfg
    if not cfg.cpuload_enabled:
        return False
    th = cpuload_threshold(cfg)
    if ctx.cpuload_latched:
        if ctx.cpu_load <= th - cfg.cpuload_hysteresis:
            ctx.cpuload_latched = False
    elif ctx.cpu_load >= th:
        ctx.cpuload_latched = True
    return ctx.cpuload_latched


def render_cpuload_high(ctx: Context) -> RGB:
    level = 0.3 + 0.7 * (math.sin(ctx.now * math.pi / 1.5) * 0.5 + 0.5)  # bernstein, langsamer Puls
    return (level, level * 0.4, 0.0)


def render_ok(ctx: Context) -> RGB:
    cfg = ctx.cfg
    level = cfg.green_active if ctx.disk_active else cfg.green_idle
    return (0.0, level, 0.0)


STATUSES: list[StatusDef] = [
    StatusDef("overtemp",       100, is_overtemp,        render_overtemp),
    StatusDef("smart_warn",      90, is_smart_failed,    render_smart_failed),
    StatusDef("fan_warn",        85, is_fan_warn,        render_fan_warn),
    StatusDef("network_down",    80, is_network_down,    render_network_down),
    StatusDef("backup_failed",   70, is_backup_failed,   render_backup_failed),
    StatusDef("diskspace_low",   60, is_diskspace_low,   render_diskspace_low),
    StatusDef("backup_running",  40, is_backup_running,  render_backup_running),
    StatusDef("cpuload_high",    30, is_cpuload_high,    render_cpuload_high),
    StatusDef("ok",               0, lambda c: True,     render_ok),
]


def current_status(ctx: Context) -> StatusDef:
    return max((s for s in STATUSES if s.condition(ctx)), key=lambda s: s.priority)


# ============================================================================
# OLED-Display (SSD1306, 128x32, I2C)
# ============================================================================
#
# Zeigt parallel zur LED Klartext: IP, CPU-Temp+Last, RAM und den aktuellen
# Status. Laeuft im selben Prozess wie die LED, aber nur 1x/Sekunde aktualisiert.

STATUS_TEXT = {
    "overtemp": "UEBERTEMPERATUR!",
    "smart_warn": "SMART-Fehler!",
    "fan_warn": "Luefter-Warnung!",
    "network_down": "Kein Netzwerk",
    "backup_failed": "Backup-Fehler!",
    "diskspace_low": "Speicher voll!",
    "backup_running": "Backup laeuft...",
    "cpuload_high": "CPU-Last hoch",
    "ok": "Normalbetrieb",
}


def get_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))   # sendet nichts, waehlt nur die Route -> Quell-IP
        return s.getsockname()[0]
    except OSError:
        return "n/a"
    finally:
        s.close()


def get_hostname() -> str:
    try:
        return socket.gethostname() or "n/a"
    except OSError:
        return "n/a"


def get_mem_mb() -> tuple[int, int]:
    try:
        info = {}
        with open("/proc/meminfo") as f:
            for line in f:
                key, _, val = line.partition(":")
                info[key] = int(val.strip().split()[0])  # kB
        total = info["MemTotal"] // 1024
        avail = info.get("MemAvailable", info.get("MemFree", 0)) // 1024
        return max(total - avail, 0), total
    except (OSError, KeyError, ValueError, IndexError):
        return 0, 0


def get_uptime_s() -> float:
    try:
        with open("/proc/uptime") as f:
            return float(f.read().split()[0])
    except (OSError, ValueError, IndexError):
        return 0.0


def fmt_uptime(seconds: float) -> str:
    s = int(seconds)
    d, h, m = s // 86400, (s % 86400) // 3600, (s % 3600) // 60
    if d:
        return f"{d}d{h}h"
    if h:
        return f"{h}h{m}m"
    return f"{m}m"


def fmt_rate(bps: float) -> str:
    units = ("B/s", "K/s", "M/s", "G/s")
    i = 0
    while bps >= 1024 and i < len(units) - 1:
        bps /= 1024
        i += 1
    return f"{bps:.0f}{units[i]}" if i == 0 else f"{bps:.1f}{units[i]}"


def oled_fields(ctx: Context, status: StatusDef) -> list[tuple[str, str]]:
    """Geordnete (Label, Wert)-Paare = die grossen Einzelseiten ('Steps' per Taster).
    Reihenfolge: zuerst die Version, dann IP gefolgt vom Hostnamen, danach der Rest.
    Optionale Felder haengen von den aktivierten Funktionen ab (siehe oled_big_page_count).
    Die Uebersicht (Seite 0) ist davon unabhaengig (siehe oled_lines)."""
    cfg = ctx.cfg
    used, total = get_mem_mb()
    fields: list[tuple[str, str]] = [
        ("Ver", __version__),
        ("IP", get_ip()),
        ("Host", get_hostname()),
        ("CPU", f"{ctx.temp_c:.0f}C L{ctx.cpu_load:.2f}"),
        ("RAM", f"{used}/{total}MB"),
        ("Status", STATUS_TEXT.get(status.name, status.name)),
        ("Up", fmt_uptime(get_uptime_s())),
    ]
    if cfg.net_throughput_enabled:
        fields.append(("Net", f"v{fmt_rate(ctx.net_rx_rate)} ^{fmt_rate(ctx.net_tx_rate)}"))
    if cfg.smart_enabled:
        fields.append(("Disk", f"{ctx.disk_temp_c:.0f}C" if ctx.disk_temp_c is not None else "n/a"))
    if cfg.fan_enabled:
        if ctx.fan_rpm is not None:
            fan_val = f"{ctx.fan_rpm}rpm"
        elif ctx.fan_level is not None:
            fan_val = f"St.{ctx.fan_level}/{ctx.fan_max_level}"   # Stufe (z. B. PoE-HAT)
        else:
            fan_val = "n/a"
        fields.append(("Fan", fan_val))
    return fields


def oled_big_page_count(cfg: Config) -> int:
    """Anzahl grosser Einzelseiten - haengt nur von cfg ab, daher stabil zur Laufzeit."""
    n = 7  # Ver, IP, Host, CPU, RAM, Status, Up
    n += int(cfg.net_throughput_enabled)
    n += int(cfg.smart_enabled)
    n += int(cfg.fan_enabled)
    return n


def oled_page_count(cfg: Config) -> int:
    """Gesamtzahl Seiten: Uebersicht (0) + grosse Einzelseiten."""
    return 1 + oled_big_page_count(cfg)


def oled_lines(ctx: Context, status: StatusDef) -> list[str]:
    """Die vier Textzeilen der Uebersicht (Seite 0): IP, CPU, RAM, Status-Text.
    Bewusst unabhaengig von der Reihenfolge der Einzelseiten (Werte per Label aus
    oled_fields, damit Format und Uebersicht konsistent bleiben)."""
    f = dict(oled_fields(ctx, status))
    return [
        f"IP {f.get('IP', '')}",
        f"CPU {f.get('CPU', '')}",
        f"RAM {f.get('RAM', '')}",
        f.get("Status", ""),
    ]


class OledStatus:
    """Echtes SSD1306-OLED ueber I2C (Adafruit PiOLED, Blinka + PIL)."""

    _TTF_CANDIDATES = (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    )

    def __init__(self, cfg: Config):
        from board import SCL, SDA
        import busio
        import adafruit_ssd1306
        from PIL import Image, ImageDraw, ImageFont
        i2c = busio.I2C(SCL, SDA)
        self._disp = adafruit_ssd1306.SSD1306_I2C(cfg.oled_width, cfg.oled_height, i2c, addr=cfg.oled_addr)
        self._w, self._h = cfg.oled_width, cfg.oled_height
        self._img = Image.new("1", (self._w, self._h))
        self._draw = ImageDraw.Draw(self._img)
        self._font = ImageFont.load_default()
        self._ttf = next((p for p in self._TTF_CANDIDATES if os.path.exists(p)), None)
        self._big_cache: dict[int, object] = {}
        self._poll = cfg.oled_poll_s
        self._last = 0.0
        self._disp.fill(0)
        self._disp.show()

    def _fit_font(self, text: str, max_w: int, max_h: int = 24, lo: int = 10, hi: int = 28):
        """Groesste TTF-Groesse, bei der text in max_w x max_h passt (gecacht)."""
        if not self._ttf:
            return self._font
        from PIL import ImageFont
        size = hi
        while size >= lo:
            f = self._big_cache.get(size)
            if f is None:
                f = ImageFont.truetype(self._ttf, size)
                self._big_cache[size] = f
            w = self._draw.textlength(text, font=f)
            bbox = f.getbbox(text)
            h = bbox[3] - bbox[1]
            if w <= max_w and h <= max_h:
                return f
            size -= 1
        return self._big_cache.get(lo, self._font)

    def update(self, ctx: Context, status: StatusDef, page: int = 0, force: bool = False) -> None:
        if not force and ctx.now - self._last < self._poll:
            return
        self._last = ctx.now
        d = self._draw
        d.rectangle((0, 0, self._w, self._h), outline=0, fill=0)
        if page <= 0:
            for i, text in enumerate(oled_lines(ctx, status)):
                d.text((0, -2 + i * 8), text, font=self._font, fill=255)
        else:
            label, value = oled_fields(ctx, status)[page - 1]
            d.text((0, -2), label, font=self._font, fill=255)        # kleines Label oben
            big = self._fit_font(value, self._w - 2, max_h=22)
            d.text((self._w // 2, 20), value, font=big, fill=255, anchor="mm")
        self._disp.image(self._img)
        self._disp.show()

    def message(self, text: str) -> None:
        """Sofort eine zentrierte Meldung anzeigen (z. B. 'Neustart...')."""
        d = self._draw
        d.rectangle((0, 0, self._w, self._h), outline=0, fill=0)
        big = self._fit_font(text, self._w - 2, max_h=24)
        d.text((self._w // 2, self._h // 2), text, font=big, fill=255, anchor="mm")
        self._disp.image(self._img)
        self._disp.show()
        self._last = 0.0  # naechster regulaerer Frame zeichnet wieder normal

    def close(self) -> None:
        try:
            self._disp.fill(0)
            self._disp.show()
        except Exception:
            pass


class ConsoleOled:
    """OLED-Ersatz fuer --simulate: gibt die Seiten im Terminal aus."""

    def __init__(self, cfg: Config):
        self._poll = cfg.oled_poll_s
        self._last = 0.0
        self._prev: str | None = None

    def update(self, ctx: Context, status: StatusDef, page: int = 0, force: bool = False) -> None:
        if not force and ctx.now - self._last < self._poll:
            return
        self._last = ctx.now
        if page <= 0:
            out = "  |  ".join(oled_lines(ctx, status))
        else:
            fields = oled_fields(ctx, status)
            label, value = fields[page - 1]
            out = f"[Seite {page}/{len(fields)}] {label}: {value}  (gross)"
        if out != self._prev:
            self._prev = out
            print("  [OLED] " + out)

    def message(self, text: str) -> None:
        self._prev = None
        print("  [OLED] >>> " + text)

    def close(self) -> None:
        pass


# ============================================================================
# Taster (kurz: Seite weiter, lang >= 5 s: Neustart)
# ============================================================================


class ButtonBase:
    """Zeit-Zustandsautomat fuer einen Taster (gedrueckt = True)."""

    def __init__(self, cfg: Config):
        self._long = cfg.button_long_press_s
        self._debounce = cfg.button_debounce_s
        self._since: float | None = None
        self._long_fired = False

    def _is_pressed(self, now: float) -> bool:
        raise NotImplementedError

    def poll(self, now: float) -> str | None:
        """Liefert 'short' (kurz losgelassen), 'long' (>= Haltedauer) oder None."""
        pressed = self._is_pressed(now)
        if pressed:
            if self._since is None:
                self._since = now
                self._long_fired = False
            elif not self._long_fired and (now - self._since) >= self._long:
                self._long_fired = True
                return "long"
        else:
            if self._since is not None:
                held = now - self._since
                self._since = None
                if not self._long_fired and self._debounce <= held < self._long:
                    return "short"
        return None

    def close(self) -> None:
        pass


class Button(ButtonBase):
    """Echter GPIO-Taster ueber Blinka digitalio (interner Pull-up)."""

    def __init__(self, cfg: Config):
        super().__init__(cfg)
        import board
        import digitalio
        self._io = digitalio.DigitalInOut(getattr(board, f"D{cfg.button_pin}"))
        self._io.direction = digitalio.Direction.INPUT
        self._io.pull = digitalio.Pull.UP

    def _is_pressed(self, now: float) -> bool:
        return not self._io.value   # Pull-up: gedrueckt = LOW

    def close(self) -> None:
        try:
            self._io.deinit()
        except Exception:
            pass


class SimButton(ButtonBase):
    """Simulierter Taster: kurzer Druck (0,15 s) alle 4 s - nur fuer --simulate."""

    def __init__(self, cfg: Config):
        super().__init__(cfg)
        self._t0 = time.monotonic()

    def _is_pressed(self, now: float) -> bool:
        return (now - self._t0) % 4.0 < 0.15


def make_button(cfg: Config, simulate: bool):
    """Erzeugt den Taster. Fehlt er/die Hardware, laeuft alles ohne Taster weiter."""
    if not cfg.button_enabled:
        return None
    try:
        return SimButton(cfg) if simulate else Button(cfg)
    except Exception as e:  # noqa: BLE001
        print(f"Taster nicht verfuegbar ({e}) - fahre ohne Taster fort", file=sys.stderr)
        return None


def do_reboot(simulate: bool) -> None:
    if simulate:
        print("  [SYSTEM] Neustart angefordert (in Simulation nur Hinweis)")
        return
    subprocess.run(["systemctl", "reboot"], check=False)


# ============================================================================
# Hauptprogramm
# ============================================================================


def make_driver(cfg: Config) -> LedDriver:
    if cfg.led_type == "analog":
        return AnalogRgbLed(cfg)
    if cfg.led_type == "ws2812":
        return NeoPixelLed(cfg)
    if cfg.led_type == "ws2812-spi":
        return NeoPixelSpiLed(cfg)
    return ConsoleLed(cfg)


def make_oled(cfg: Config, simulate: bool):
    """Erzeugt das OLED-Objekt. Schlaegt es fehl (kein I2C/OLED), laeuft die LED trotzdem weiter."""
    if not cfg.oled_enabled:
        return None
    try:
        return ConsoleOled(cfg) if simulate else OledStatus(cfg)
    except Exception as e:  # noqa: BLE001
        print(f"OLED nicht verfuegbar ({e}) - fahre nur mit LED fort", file=sys.stderr)
        return None


def run(cfg: Config, simulate: bool = False) -> None:
    if simulate:
        cfg.led_type = "console"
        # Im Simulationsbetrieb alle Funktionen aktivieren, damit die Demo sie zeigt
        cfg.smart_enabled = True
        cfg.fan_enabled = True
        if cfg.fan_warn_below_rpm <= 0:
            cfg.fan_warn_below_rpm = 500

    driver = make_driver(cfg)
    oled = make_oled(cfg, simulate)
    button = make_button(cfg, simulate)

    if simulate:
        temp = SimTemperature(cfg)
        disk = SimDisk(cfg)
        net = SimNetwork(cfg)
        backup = SimBackup(cfg)
        cpuload = SimCpuLoad(cfg)
        diskspace = SimDiskSpace(cfg)
        fan = SimFan(cfg)
        smart = SimSmart(cfg)
        netio = SimNetThroughput(cfg)
    else:
        temp = TemperatureSensor(cfg)
        disk = DiskActivity(cfg)
        net = BackgroundValue(
            lambda: not check_internet(cfg.net_check_host, cfg.net_check_port, cfg.net_check_timeout),
            interval_s=cfg.net_poll_s, initial=False,
        )
        backup = BackupStatus(cfg)
        cpuload = CpuLoadSensor(cfg) if cfg.cpuload_enabled else None
        diskspace = DiskSpaceSensor(cfg) if cfg.diskspace_enabled else None
        fan = FanSensor(cfg) if cfg.fan_enabled else None
        smart = BackgroundValue(lambda: smart_probe(cfg), interval_s=cfg.smart_poll_s,
                                initial={"failed": False, "temp_c": None}) if cfg.smart_enabled else None
        netio = NetThroughput(cfg) if cfg.net_throughput_enabled else None

    ctx = Context(cfg=cfg)
    page_count = oled_page_count(cfg)
    stop = {"flag": False}

    def handle(_sig, _frame):
        stop["flag"] = True

    signal.signal(signal.SIGINT, handle)
    signal.signal(signal.SIGTERM, handle)

    page = 0
    page_since = time.monotonic()
    rebooting = False
    start = time.monotonic()
    try:
        while not stop["flag"]:
            ctx.now = time.monotonic()

            if rebooting:                # Anzeige eingefroren bis SIGTERM/Neustart
                time.sleep(cfg.tick_s)
                continue

            ctx.temp_c = temp.read(ctx.now)
            ctx.disk_active = disk.is_active(ctx.now)
            ctx.network_down = net.value
            ctx.backup_state = backup.read(ctx.now)
            if cpuload is not None:
                ctx.cpu_load = cpuload.read(ctx.now)
            if diskspace is not None:
                ctx.disk_free_pct = diskspace.read(ctx.now)
            if fan is not None:
                fan.read(ctx.now)
                ctx.fan_rpm = fan.rpm
                ctx.fan_level = fan.level
                ctx.fan_max_level = fan.max_level
                rpm_warn = (cfg.fan_warn_below_rpm > 0 and ctx.fan_rpm is not None
                            and ctx.fan_rpm < cfg.fan_warn_below_rpm)
                level_warn = (cfg.fan_warn_at_max and ctx.fan_level is not None
                              and ctx.fan_max_level is not None and ctx.fan_level >= ctx.fan_max_level)
                ctx.fan_failed = rpm_warn or level_warn
            if smart is not None:
                s = smart.value
                ctx.smart_failed = s["failed"]
                ctx.disk_temp_c = s["temp_c"]
            if netio is not None:
                ctx.net_rx_rate, ctx.net_tx_rate = netio.read(ctx.now)

            status = current_status(ctx)
            driver.set_color(status.render(ctx))

            force_oled = False
            if button is not None:
                event = button.poll(ctx.now)
                if event == "short":
                    page = (page + 1) % page_count
                    page_since = ctx.now
                    force_oled = True
                elif event == "long":
                    rebooting = True
                    driver.set_color((1.0, 0.0, 0.0))   # rot
                    if oled is not None:
                        try:
                            oled.message("Neustart...")
                        except Exception:
                            pass
                    # Meldung erst sichtbar stehen lassen, dann neu starten
                    if cfg.button_reboot_message_s > 0:
                        time.sleep(cfg.button_reboot_message_s)
                    do_reboot(simulate)
                    if simulate:
                        break
                    continue

            # Auto-Ruecksprung auf die Uebersicht
            if page != 0 and (ctx.now - page_since) >= cfg.oled_page_timeout_s:
                page = 0
                force_oled = True

            if oled is not None:
                try:
                    oled.update(ctx, status, page=page, force=force_oled)
                except Exception:        # OLED-Fehler darf die LED nie stoppen
                    pass

            if cfg.duration_s and (ctx.now - start) >= cfg.duration_s:
                break
            time.sleep(cfg.tick_s)
    finally:
        net.stop()
        if smart is not None and hasattr(smart, "stop"):
            smart.stop()
        driver.set_color(OFF)
        driver.close()
        if oled is not None:
            oled.close()
        if button is not None:
            button.close()


# ============================================================================
# Konfigurations-Assistent  --  status_led.py --setup
# ============================================================================
#
# Geführter Textdialog, der eine config.toml erzeugt. Bewusst ohne Zusatzpakete
# (nur input()), damit er überall laeuft (auch ueber 'curl | sudo bash' via
# Eingabeumleitung </dev/tty).


def _ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default != "" else ""
    try:
        ans = input(f"{prompt}{suffix}: ").strip()
    except EOFError:
        ans = ""
    return ans or default


def _ask_yesno(prompt: str, default: bool) -> bool:
    d = "J/n" if default else "j/N"
    ans = _ask(f"{prompt} ({d})", "").lower()
    if not ans:
        return default
    return ans in ("j", "ja", "y", "yes")


def _ask_choice(prompt: str, options: list[str], default: str) -> str:
    print(f"\n{prompt}")
    for i, opt in enumerate(options, 1):
        marker = " (Standard)" if opt == default else ""
        print(f"  {i}) {opt}{marker}")
    while True:
        ans = _ask("Auswahl (Nummer)", str(options.index(default) + 1))
        if ans.isdigit() and 1 <= int(ans) <= len(options):
            return options[int(ans) - 1]
        print("  Bitte eine gueltige Nummer eingeben.")


def _ask_float(prompt: str, default: float) -> float:
    while True:
        ans = _ask(prompt, str(default))
        try:
            return float(ans)
        except ValueError:
            print("  Bitte eine Zahl eingeben.")


def _ask_int(prompt: str, default: int) -> int:
    while True:
        ans = _ask(prompt, str(default))
        try:
            return int(ans)
        except ValueError:
            print("  Bitte eine ganze Zahl eingeben.")


def detect_i2c_addresses() -> list[str]:
    """Liefert die auf dem I2C-Bus gefundenen Adressen (z. B. ['0x3c'])."""
    try:
        out = subprocess.run(["i2cdetect", "-y", "1"],
                             capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    found: list[str] = []
    for line in out.splitlines()[1:]:
        _, _, rest = line.partition(":")
        for tok in rest.split():
            if tok not in ("--", "UU"):
                found.append("0x" + tok)
    return found


def _render_config_toml(s: dict) -> str:
    def b(v):  # bool -> TOML
        return "true" if v else "false"
    lines = [
        "# Erzeugt von status_led.py --setup",
        f'led_type = "{s["led_type"]}"',
        "",
        "[led]",
        f'ws_count       = {s["ws_count"]}',
        f'ws_brightness  = {s["ws_brightness"]}',
        f'ws_pixel_order = "{s["ws_pixel_order"]}"',
        "",
        "[oled]",
        f"enabled = {b(s['oled_enabled'])}",
        f"addr    = {s['oled_addr']}",
        "",
        "[button]",
        f"enabled      = {b(s['button_enabled'])}",
        "",
        "[temp]",
        f"threshold_c = {s['temp_threshold_c']}",
        "",
        "[network]",
        f'check_host = "{s["net_check_host"]}"',
        "",
        "[cpuload]",
        f"enabled = {b(s['cpuload_enabled'])}",
        "",
        "[diskspace]",
        f"enabled          = {b(s['diskspace_enabled'])}",
        f"min_free_percent = {s['diskspace_min_free_percent']}",
        "",
        "[smart]",
        f"enabled = {b(s['smart_enabled'])}",
        "",
        "[fan]",
        f"enabled        = {b(s['fan_enabled'])}",
        f"warn_below_rpm = {s['fan_warn_below_rpm']}",
        "",
    ]
    return "\n".join(lines)


def run_setup(config_path: str) -> int:
    """Interaktiver Assistent: fragt die wichtigsten Optionen ab und schreibt config.toml."""
    d = Config()  # Defaults als Vorgabewerte
    print("=" * 60)
    print(" Status-LED + OLED  --  Konfigurations-Assistent")
    print("=" * 60)
    print("Enter uebernimmt jeweils den Vorgabewert in [Klammern].\n")

    s: dict = {}
    s["led_type"] = _ask_choice(
        "LED-Typ?", ["ws2812", "ws2812-spi", "analog", "console"], d.led_type)
    is_ws = s["led_type"].startswith("ws2812")
    s["ws_count"] = _ask_int("Anzahl LEDs", d.ws_count) if is_ws else d.ws_count
    s["ws_brightness"] = _ask_float("Helligkeit 0..1", d.ws_brightness) if is_ws else d.ws_brightness
    s["ws_pixel_order"] = (_ask_choice("Farbreihenfolge?", ["GRB", "RGB"], d.ws_pixel_order)
                           if is_ws else d.ws_pixel_order)

    # OLED
    s["oled_enabled"] = _ask_yesno("OLED-Display verwenden?", d.oled_enabled)
    addr = d.oled_addr
    if s["oled_enabled"]:
        found = detect_i2c_addresses()
        if found:
            print(f"  Auf dem I2C-Bus gefunden: {', '.join(found)}")
            default_addr = found[0] if found else hex(d.oled_addr)
        else:
            print("  (i2cdetect fand nichts - Standardadresse verwenden oder spaeter pruefen)")
            default_addr = hex(d.oled_addr)
        raw = _ask("OLED I2C-Adresse (hex)", default_addr)
        try:
            addr = int(raw, 16) if isinstance(raw, str) else int(raw)
        except ValueError:
            addr = d.oled_addr
    s["oled_addr"] = hex(addr)

    s["button_enabled"] = _ask_yesno("Taster an GPIO17 verwenden?", d.button_enabled)
    s["temp_threshold_c"] = _ask_float("Uebertemperatur-Schwelle (Grad C)", d.temp_threshold_c)
    s["net_check_host"] = _ask("Host fuer Netzwerk-Check (LAN: Gateway-IP)", d.net_check_host)

    print("\n-- Optionale Zustaende --")
    s["cpuload_enabled"] = _ask_yesno("Warnung bei hoher CPU-Last?", d.cpuload_enabled)
    s["diskspace_enabled"] = _ask_yesno("Warnung bei wenig Speicherplatz?", d.diskspace_enabled)
    s["diskspace_min_free_percent"] = (
        _ask_float("  Schwelle: Warnung unter ... % frei", d.diskspace_min_free_percent)
        if s["diskspace_enabled"] else d.diskspace_min_free_percent)
    s["smart_enabled"] = _ask_yesno("SMART-Festplattengesundheit? (braucht smartmontools + root)",
                                    d.smart_enabled)
    s["fan_enabled"] = _ask_yesno("Luefterdrehzahl/-warnung? (braucht hwmon-Tacho)", d.fan_enabled)
    s["fan_warn_below_rpm"] = (
        _ask_int("  Warnung unter ... U/min (0 = nur anzeigen)", d.fan_warn_below_rpm)
        if s["fan_enabled"] else d.fan_warn_below_rpm)

    toml_text = _render_config_toml(s)
    print("\n" + "-" * 60)
    print(toml_text)
    print("-" * 60)
    if not _ask_yesno(f"Diese Konfiguration nach {config_path} schreiben?", True):
        print("Abgebrochen - nichts geschrieben.")
        return 1

    path = Path(config_path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            backup = path.with_suffix(path.suffix + ".bak")
            backup.write_text(path.read_text())
            print(f"Vorhandene Datei gesichert: {backup}")
        path.write_text(toml_text)
    except OSError as e:
        print(f"Fehler beim Schreiben ({e}). Mit 'sudo' erneut versuchen?", file=sys.stderr)
        return 1
    print(f"Geschrieben: {path}")
    print("Dienst neu starten mit:  sudo systemctl restart status-led.service")
    return 0


def run_diag(config_path: str) -> int:
    """Hardware-Diagnose: zeigt, was fuer OLED, SMART und Luefter erkannt wird.
    Hilfreich, wenn Werte fehlen (z. B. SMART/Luefter)."""
    print(f"status-led {__version__}  --  Diagnose")
    try:
        model = Path("/proc/device-tree/model").read_text().strip("\x00").strip()
    except OSError:
        model = "unbekannt"
    print(f"Modell: {model}")
    print(f"Konfig: {config_path} ({'vorhanden' if Path(config_path).exists() else 'fehlt -> Defaults'})")

    print("\n[I2C]")
    if shutil.which("i2cdetect") is None:
        print("  i2cdetect nicht installiert (apt install i2c-tools)")
    else:
        addrs = detect_i2c_addresses()
        print(f"  gefundene Adressen: {', '.join(addrs) if addrs else 'keine'}")

    print("\n[SMART]")
    if shutil.which("smartctl") is None:
        print("  smartctl nicht installiert (apt install smartmontools)")
    else:
        devices = smart_scan()
        if not devices:
            print("  keine Geraete gefunden (SD-Karten haben kein SMART; USB-SSD ggf. '-d sat')")
        for dev in devices:
            data = _smartctl_json(dev)
            passed = data.get("smart_status", {}).get("passed")
            temp = data.get("temperature", {}).get("current")
            status = "ok" if passed else ("FEHLER" if passed is False else "n/a")
            print(f"  {dev}: Status={status}, Temp={temp if temp is not None else 'n/a'}")

    print("\n[Luefter]")
    rpm_path = find_fan_rpm_path()
    cool = find_fan_cooling_dev()
    if rpm_path:
        try:
            rpm = Path(rpm_path).read_text().strip()
        except OSError:
            rpm = "?"
        print(f"  hwmon-Tacho: {rpm_path} = {rpm} rpm")
    elif cool:
        try:
            ctype = Path(cool + "/type").read_text().strip()
            cur = Path(cool + "/cur_state").read_text().strip()
            mx = Path(cool + "/max_state").read_text().strip()
            print(f"  cooling_device: {cool} (type={ctype}) Stufe {cur}/{mx}")
            print("  -> kein RPM-Tacho; in der Config 'fan.warn_at_max = true' fuer Warnung bei Dauer-Maximalstufe")
        except OSError:
            print(f"  cooling_device: {cool} (nicht lesbar)")
    else:
        print("  weder hwmon-Tacho noch cooling_device gefunden")
        print("  -> PoE-/PoE+-HAT-Luefter werden von der Firmware geregelt und sind fuer")
        print("     Linux nur sichtbar, wenn das Overlay geladen ist. Beim offiziellen HAT:")
        print("     'dtoverlay=rpi-poe' (PoE) bzw. 'dtoverlay=rpi-poe-plus' (PoE+) in")
        print("     /boot/firmware/config.txt eintragen, neu starten, dann erneut --diag.")
        print("     Fremd-HATs mit eigener Luefterregelung bleiben fuer Linux unsichtbar.")
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="RGB-Status-LED + OLED fuer Raspberry Pi 4")
    p.add_argument("--version", action="version", version=f"status-led {__version__}")
    p.add_argument("--diag", action="store_true",
                   help="Hardware-Diagnose (OLED/SMART/Luefter) ausgeben und beenden")
    p.add_argument("--setup", action="store_true",
                   help="interaktiver Konfigurations-Assistent (schreibt config.toml)")
    p.add_argument("--simulate", action="store_true", help="ohne Hardware im Terminal testen")
    p.add_argument("--config", default=DEFAULT_CONFIG_PATH,
                   help=f"Pfad zur TOML-Konfiguration (Standard: {DEFAULT_CONFIG_PATH})")
    p.add_argument("--led-type", choices=["analog", "ws2812", "ws2812-spi", "console"],
                   help="LED-Treiber ueberschreiben")
    p.add_argument("--temp-threshold", type=float, help="Temperatur-Schwelle in Grad C")
    p.add_argument("--no-oled", action="store_true", help="OLED deaktivieren")
    p.add_argument("--no-button", action="store_true", help="Taster deaktivieren")
    p.add_argument("--duration", type=float, help="Laufzeit in Sekunden (0 = endlos)")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    if args.setup:
        sys.exit(run_setup(args.config))
    if args.diag:
        sys.exit(run_diag(args.config))
    # Reihenfolge: Defaults < Konfigurationsdatei < CLI-Argumente
    cfg = load_config(args.config, required=(args.config != DEFAULT_CONFIG_PATH))
    if args.led_type:
        cfg.led_type = args.led_type
    if args.temp_threshold is not None:
        cfg.temp_threshold_c = args.temp_threshold
    if args.no_oled:
        cfg.oled_enabled = False
    if args.no_button:
        cfg.button_enabled = False
    if args.duration is not None:
        cfg.duration_s = args.duration
    run(cfg, simulate=args.simulate)


if __name__ == "__main__":
    main()
