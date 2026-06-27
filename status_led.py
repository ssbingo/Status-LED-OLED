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
import math
import os
import random
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

    # --- Backup-Status (wird aus einer Status-Datei gelesen) ---
    backup_status_path: str = "/run/status-led/backup"
    backup_poll_s: float = 1.0

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


def render_ok(ctx: Context) -> RGB:
    cfg = ctx.cfg
    level = cfg.green_active if ctx.disk_active else cfg.green_idle
    return (0.0, level, 0.0)


STATUSES: list[StatusDef] = [
    StatusDef("overtemp",       100, is_overtemp,       render_overtemp),
    StatusDef("network_down",    80, is_network_down,   render_network_down),
    StatusDef("backup_failed",   70, is_backup_failed,  render_backup_failed),
    StatusDef("backup_running",  40, is_backup_running, render_backup_running),
    StatusDef("ok",               0, lambda c: True,    render_ok),
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
    "network_down": "Kein Netzwerk",
    "backup_failed": "Backup-Fehler!",
    "backup_running": "Backup laeuft...",
    "ok": "Normalbetrieb",
}

OLED_BIG_PAGES = 4                 # Anzahl Einzelzeilen-Seiten (gross)
OLED_PAGES = 1 + OLED_BIG_PAGES    # Seite 0 = Uebersicht, 1..4 = Einzelzeilen


def get_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))   # sendet nichts, waehlt nur die Route -> Quell-IP
        return s.getsockname()[0]
    except OSError:
        return "n/a"
    finally:
        s.close()


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


def oled_fields(ctx: Context, status: StatusDef) -> list[tuple[str, str]]:
    """Vier (Label, Wert)-Paare - Basis fuer Uebersicht und Einzelseiten."""
    used, total = get_mem_mb()
    try:
        load1 = os.getloadavg()[0]
    except OSError:
        load1 = 0.0
    return [
        ("IP", get_ip()),
        ("CPU", f"{ctx.temp_c:.1f}C  L{load1:.2f}"),
        ("RAM", f"{used}/{total}MB"),
        ("Status", STATUS_TEXT.get(status.name, status.name)),
    ]


def oled_lines(ctx: Context, status: StatusDef) -> list[str]:
    """Die vier Textzeilen der Uebersicht (Seite 0)."""
    f = oled_fields(ctx, status)
    return [f"{f[0][0]} {f[0][1]}", f"{f[1][0]} {f[1][1]}", f"{f[2][0]} {f[2][1]}", f[3][1]]


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
            label, value = oled_fields(ctx, status)[page - 1]
            out = f"[Seite {page}/{OLED_PAGES - 1}] {label}: {value}  (gross)"
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

    driver = make_driver(cfg)
    oled = make_oled(cfg, simulate)
    button = make_button(cfg, simulate)

    if simulate:
        temp = SimTemperature(cfg)
        disk = SimDisk(cfg)
        net = SimNetwork(cfg)
        backup = SimBackup(cfg)
    else:
        temp = TemperatureSensor(cfg)
        disk = DiskActivity(cfg)
        net = BackgroundValue(
            lambda: not check_internet(cfg.net_check_host, cfg.net_check_port, cfg.net_check_timeout),
            interval_s=cfg.net_poll_s, initial=False,
        )
        backup = BackupStatus(cfg)

    ctx = Context(cfg=cfg)
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

            status = current_status(ctx)
            driver.set_color(status.render(ctx))

            force_oled = False
            if button is not None:
                event = button.poll(ctx.now)
                if event == "short":
                    page = (page + 1) % OLED_PAGES
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
        driver.set_color(OFF)
        driver.close()
        if oled is not None:
            oled.close()
        if button is not None:
            button.close()


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="RGB-Status-LED + OLED fuer Raspberry Pi 4")
    p.add_argument("--simulate", action="store_true", help="ohne Hardware im Terminal testen")
    p.add_argument("--led-type", choices=["analog", "ws2812", "ws2812-spi", "console"],
                   help="LED-Treiber ueberschreiben")
    p.add_argument("--temp-threshold", type=float, help="Temperatur-Schwelle in Grad C")
    p.add_argument("--no-oled", action="store_true", help="OLED deaktivieren")
    p.add_argument("--no-button", action="store_true", help="Taster deaktivieren")
    p.add_argument("--duration", type=float, help="Laufzeit in Sekunden (0 = endlos)")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    cfg = Config()
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
