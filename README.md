# RGB Status LED (WS2812B) + OLED Display (SSD1306) for Raspberry Pi 4

---

<p align="center">
  <a href="https://www.buymeacoffee.com/ssbingo"><img src="https://img.buymeacoffee.com/button-api/?text=Buy%20me%20a%20coffee&emoji=&slug=ssbingo&button_colour=FFDD00&font_colour=000000&font_family=Cookie&outline_colour=000000&coffee_colour=ffffff" /></a>
</p>

---

**English** · [Deutsch](doc/de/README.md)

A status LED (WS2812B) plus a 128×32 OLED (SSD1306 / Adafruit PiOLED) on a Raspberry Pi 4. The LED shows the system state by colour, the OLED shows the same state in plain text (IP, CPU, RAM, status). Both are driven by a single script (`status_led.py`) and one service. Near the end there is a hands-on **Troubleshooting** chapter with the pitfalls from a real-world setup, followed by the changelog and the license.

## What the LED shows

- **LED green** — normal operation; bright flicker on disk activity
- **LED red/green alternating every second** — over-temperature (threshold configurable)
- **LED white blinking fast** — SMART disk health failed (optional)
- **LED orange blinking** — fan warning, e.g. fan stalled (optional)
- **LED blue blinking** — no network connection
- **LED magenta blinking** — backup failed
- **LED yellow blinking** — low free disk space
- **LED cyan pulsing** — backup running
- **LED amber pulsing** — high CPU load
- **OLED** — continuously shows IP address, CPU temperature + load, RAM usage and the state in plain text; further pages (push button) show the installed version, hostname, uptime, network throughput, and — if enabled — disk temperature and fan speed

> All states and thresholds are configurable in `/etc/status-led/config.toml` (see section 8). The optional SMART and fan states are off by default.

---

## Quick install (recommended)

On the Raspberry Pi, run a single command — it installs everything (sources, libraries in a virtual environment, systemd service), enables I2C, and then starts an interactive **configuration wizard** that writes your `config.toml`:

```bash
curl -fsSL https://raw.githubusercontent.com/ssbingo/Status-LED-OLED/main/install.sh | sudo bash
```

What it does: checks it is a real Pi → installs `git`, a Python venv and the libraries → clones the repo to `/opt/status-led` → asks you the relevant settings (LED type, count, OLED address via `i2cdetect`, thresholds, optional states) → enables I2C (and SPI for the SPI variant), disables onboard audio for PWM → creates and starts the service. A reboot is offered at the end if interface changes need it.

Afterwards a `status-led` helper command is available:

```bash
status-led status     # service status
status-led logs        # live log
status-led setup       # re-run the configuration wizard
status-led update      # update to the latest version (see below)
status-led restart     # restart the service
status-led diag        # hardware diagnostics (OLED/SMART/fan)
```

### Update

When new changes land in the repo, update in place — your `config.toml` is kept:

```bash
sudo status-led update
```

It runs `git pull`, refreshes the libraries, restarts the service and reports the old → new version. Because every config key is optional, an existing `config.toml` keeps working after an update; new keys fall back to their defaults.

> Prefer to inspect the script first? Clone and run it instead of piping:
> `git clone https://github.com/ssbingo/Status-LED-OLED.git && sudo ./Status-LED-OLED/install.sh`

The sections below document the **manual installation** step by step — useful for understanding the details or for a custom setup.

---

## 1. Requirements

### Hardware

- Raspberry Pi 4 with Raspberry Pi OS (Bookworm) — **with a physical GPIO header**
- WS2812B LED (5050), single or as a strip
- OLED display 128×32, SSD1306, I2C (e.g. Adafruit PiOLED)
- Resistor 330–470 Ω for the LED data line
- Jumper wires; for multiple LEDs also a level shifter (74AHCT125) and a 1000 µF capacitor

### Software

- Python 3 (pre-installed on Raspberry Pi OS)
- Internet access for the one-time library installation
- The file `status_led.py` (from this repository)

---

## 2. LED connection method: PWM or SPI

The WS2812B can be driven two ways. The OLED is unaffected (separate bus).

- **PWM (GPIO18) — recommended**, especially for a single LED. The data line is driven cleanly and pulled Low when idle — "off" stays off and weak colours (dim green) stay stable. Requirement: the service runs as **root** and the onboard audio must be disabled.
- **SPI (GPIO10/MOSI)** — runs without root and without the audio conflict. **However:** the Pi leaves the SPI data line High between packets; some WS2812B read this as "all bits on" = white. Dim colours and "off" can then drift to white (see Troubleshooting). Only use this if root/audio are a problem and the LED passes the test.

> **Tip:** When in doubt, use PWM — it is the robust default for a single WS2812B on the Pi. This guide is written for PWM; the SPI steps are noted as an alternative throughout.

---

## 3. Wiring (LED + OLED)

Power off the Pi and connect both. The pins of the two devices do not overlap:

### WS2812B (LED)

- **DIN** (mind the arrow — input side) → through the **330–470 Ω resistor** to **GPIO18 / pin 12** (PWM) or GPIO10/MOSI (SPI)
- **VDD** → for a **single** LED to **3.3 V (pin 1)**; for multiple LEDs to 5 V *with* a level shifter
- **GND** → GND of the Pi (a common ground is mandatory)

> **Tip:** VDD at 3.3 V instead of 5 V is the simplest and clean permanent solution for a single LED: the Pi outputs data at 3.3 V, but a WS2812B at 5 V expects ~3.5 V for "High" — at 3.3 V VDD the level matches again. For multiple LEDs (5 V required) a level shifter (74AHCT125) is needed.

### OLED (SSD1306, I2C)

- **3V3** → pin 1  ·  **GND** → GND
- **SDA** → GPIO2 (pin 3)  ·  **SCL** → GPIO3 (pin 5)

### Push button (display + reboot)

- One leg → **GPIO17 (pin 11)**, the other leg → **GND** (e.g. pin 14)
- The internal pull-up is enabled in software (pressed = LOW), so **no resistor is needed**
- Short press cycles the display pages, a long press (≥ 5 s) reboots the Pi (see section 11)

### Combined pin overview

| Device / function         | Pin          | Signal         |
|---------------------------|--------------|----------------|
| LED – data (PWM, rec.)    | Pin 12       | GPIO18         |
| LED – data (SPI, alt.)    | Pin 19       | GPIO10 / MOSI  |
| LED – VDD (1 LED)         | Pin 1        | 3V3            |
| LED – GND                 | Pin 9 (free) | GND            |
| OLED – 3V3                | Pin 1        | 3V3            |
| OLED – SDA                | Pin 3        | GPIO2          |
| OLED – SCL                | Pin 5        | GPIO3          |
| OLED – GND                | Pin 6        | GND            |
| Button – signal           | Pin 11       | GPIO17         |
| Button – GND              | Pin 14 (free)| GND            |

*Pin 1 (3V3) powers both the OLED and the single LED — both may share it. A PiOLED usually sits on the corner pins (1/3/5 + GND), so take the LED's GND from a free pin (e.g. pin 9).*

### Raspberry Pi 4 – pinout (reference)

![Raspberry Pi 4 pinout](img/raspberrypi4-pinout.png)

*Assignment for this project: **LED data** to GPIO18 (pin 12), **LED VDD** and **OLED 3V3** to 3.3 V (pin 1), **OLED SDA** to GPIO2 (pin 3), **OLED SCL** to GPIO3 (pin 5), **push button** on GPIO17 (pin 11) against GND, common **ground** to a GND pin (e.g. pin 6, 9 or 14). SPI alternative for the LED data: GPIO10/MOSI (pin 19).*

---

## 4. Do OLED and LED work together?

**Yes.** The OLED is on the **I2C bus** (GPIO2/3), the LED on the **PWM line** (GPIO18) or the **SPI bus** (GPIO10). Separate hardware buses on different pins — no conflict. Both are driven by the same process (`status_led.py` refreshes the OLED once per second). If the OLED fails to initialise, the LED still runs, and vice versa.

**Requirement:** real GPIO/I2C/SPI interfaces of a Raspberry Pi. In an LXC container or a VM (e.g. on an x86 Proxmox host) there is no Pi header — then none of this works. Quick check whether it is a real Pi with both buses:

```bash
cat /proc/device-tree/model        # must show 'Raspberry Pi 4 ...'
ls -l /dev/i2c-1 /dev/spidev0.0    # both device files must exist
```

---

## 5. Install the script

Copy the file into a fixed location and make it executable:

```bash
sudo cp status_led.py /usr/local/bin/status_led.py
sudo chmod +x /usr/local/bin/status_led.py
```

---

## 6. Install the libraries

Each command is deliberately on **one line** — a backslash at the end of a line is not needed (and, in the middle of a line, causes errors).

### PWM (recommended)

```bash
sudo apt install -y python3-pil i2c-tools
sudo pip3 install adafruit-blinka --break-system-packages
sudo pip3 install rpi_ws281x --break-system-packages
sudo pip3 install adafruit-circuitpython-neopixel --break-system-packages
sudo pip3 install adafruit-circuitpython-ssd1306 --break-system-packages
```

### SPI (alternative)

```bash
sudo apt install -y python3-pil i2c-tools
sudo pip3 install adafruit-blinka --break-system-packages
sudo pip3 install adafruit-circuitpython-neopixel-spi --break-system-packages
sudo pip3 install adafruit-circuitpython-ssd1306 --break-system-packages
```

### Alternative — virtual environment (without touching the system Python)

```bash
sudo apt install -y python3-venv i2c-tools
sudo python3 -m venv /opt/status-led-venv
sudo /opt/status-led-venv/bin/pip install adafruit-blinka rpi_ws281x
sudo /opt/status-led-venv/bin/pip install adafruit-circuitpython-neopixel
sudo /opt/status-led-venv/bin/pip install adafruit-circuitpython-ssd1306 pillow
```

> **Note:** With the venv variant, use the interpreter from the venv in the service (section 10):
> `ExecStart=/opt/status-led-venv/bin/python /usr/local/bin/status_led.py`

> **Optional (SMART state):** to use the disk-health state, also install smartmontools — `sudo apt install -y smartmontools`. CPU load, disk space and network throughput need no extra packages (pure stdlib).

---

## 7. Enable I2C, check the OLED, (PWM:) disable audio

Enable I2C for the OLED (for the SPI LED also enable SPI):

```bash
sudo raspi-config
#  Interface Options  ->  I2C   ->  Yes
#  (SPI variant only:  Interface Options -> SPI -> Yes)
```

For the **PWM variant** the onboard audio must be off (it shares the PWM hardware, otherwise the LED flickers). In `/boot/firmware/config.txt`:

```bash
sudo sed -i 's/^dtparam=audio=on/dtparam=audio=off/' /boot/firmware/config.txt
grep audio /boot/firmware/config.txt
#  if grep shows no 'audio=off', then once:
echo "dtparam=audio=off" | sudo tee -a /boot/firmware/config.txt
```

After the interface changes, reboot and look for the OLED on the I2C bus:

```bash
sudo reboot
#  after the reboot:
sudo i2cdetect -y 1
```

> **Note:** If `3c` appears in the table, the display is detected. If yours shows `0x3d`, enter that value in the config at `oled_addr`.

---

## 8. Adjust the configuration

There are two ways to configure the script. **Recommended: a TOML file** — you keep your settings separate from the code, so a script update never overwrites them.

### Configuration file (recommended)

The script reads `/etc/status-led/config.toml` on start (override with `--config /path`). Every key is optional; missing keys fall back to the defaults. Copy the template from this repo and edit it:

```bash
sudo mkdir -p /etc/status-led
sudo cp config.example.toml /etc/status-led/config.toml
sudo nano /etc/status-led/config.toml
```

```toml
led_type = "ws2812"          # "ws2812" (PWM) | "ws2812-spi" | "analog" | "console"

[led]
ws_count       = 1           # the actual number of LEDs!
ws_brightness  = 0.50        # master brightness 0..1
ws_pixel_order = "GRB"       # standard WS2812B; rarely "RGB"

[oled]
enabled = true
addr    = 0x3C               # I2C address (see i2cdetect)

[temp]
threshold_c = 70.0           # over-temperature threshold in degrees C

[network]
check_host = "1.1.1.1"       # internet check; for LAN use the gateway IP

[button]
enabled      = true          # push button on GPIO17
long_press_s = 5.0           # hold this long -> reboot

# Optional new states (see section 13):
[cpuload]
threshold = 0.0              # 1-min load threshold; 0 = auto (= number of cores)
[diskspace]
min_free_percent = 10.0      # warn below this much free space
[smart]
enabled = false              # disk health via smartctl (needs smartmontools + root)
[fan]
enabled = false              # fan RPM/warning (needs a hwmon tach)
```

The full list of keys with comments is in [`config.example.toml`](config.example.toml). Sections (`[led]`, `[temp]`, …) group the keys; unknown keys are reported in the log and ignored. After editing, restart the service: `sudo systemctl restart status-led.service`.

- **`led_type`** — `"ws2812"` (PWM) or `"ws2812-spi"` (SPI)
- **`led.ws_count`** — **exactly** the number of your LEDs; too low → surplus LEDs keep their old value
- **`led.ws_pixel_order`** — almost always `"GRB"`; only change it if the colour test says so
- **`led.ws_brightness`** — master dimmer; WS2812B are very bright, 0.2–0.5 is usually pleasant
- **`button.pin`** — BCM pin of the push button (default GPIO17 / pin 11)
- **`button.long_press_s`** — hold time that triggers a reboot (default 5 s)
- **`oled.page_timeout_s`** — after this idle time the display returns to the overview

### Alternative: edit the defaults in the script

Without a config file, the defaults in the `Config` dataclass at the top of `status_led.py` apply. You can edit them directly (`sudo nano /usr/local/bin/status_led.py`), but the config file is cleaner and survives updates.

---

## 9. Functional test

### Without hardware (terminal)

Shows the LED states and the OLED lines in parallel in the terminal:

```bash
python3 status_led.py --simulate --duration 30
```

### Direct LED test (colours + stability, PWM)

Checks the colour order and whether dim green stays stable (with sudo, GPIO18):

```bash
sudo systemctl stop status-led.service
sudo python3 - <<'PYEOF'
import board, neopixel, time
px = neopixel.NeoPixel(board.D18, 1, brightness=0.3, auto_write=True, pixel_order="GRB")
for n,c in [("RED",(255,0,0)),("GREEN",(0,255,0)),("BLUE",(0,0,255))]:
    px.fill(c); print("commanded:", n); time.sleep(2)
print("20s dim green - stays green, then off cleanly?")
for i in range(200): px.fill((0,40,0)); time.sleep(0.1)
px.fill((0,0,0))
PYEOF
```

> **Note:** Red/green/blue must match, the dim green must stay stable and turn off cleanly at the end. For the SPI variant use `neopixel_spi.NeoPixel_SPI(board.SPI(), 1, ...)` instead.

### Service in the foreground (shows errors live)

```bash
python3 /usr/local/bin/status_led.py    # stop with Ctrl+C
```

### Unit tests (status logic)

The status logic (priorities, hysteresis, config mapping) has unit tests that run **without hardware** — they only build a `Context` and check the result. Run them from the repository:

```bash
python3 -m unittest discover -s tests
```

No extra packages are needed (stdlib `unittest` + `tomllib`). This is the quickest way to confirm a change to the states or the config loader did not break anything.

---

## 10. Autostart as a service (systemd)

One service drives the LED and the OLED together. Create the service file (template included in this repo):

```bash
sudo nano /etc/systemd/system/status-led.service
```

```ini
[Unit]
Description=RGB Status LED + OLED
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 /usr/local/bin/status_led.py
Restart=always
RestartSec=5
User=root
RuntimeDirectory=status-led
#  SPI variant: User=YOUR_USERNAME  (NOT 'pi')
#  venv: ExecStart=/opt/status-led-venv/bin/python /usr/local/bin/status_led.py

[Install]
WantedBy=multi-user.target
```

> **Tip:** Important about the user: **PWM needs `User=root`**. For the SPI variant enter a real username — current Raspberry Pi OS no longer has the former default user `pi`. A wrong/missing user leads to `status=217/USER` (see Troubleshooting).

Enable, start and check:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now status-led.service
systemctl status status-led.service
journalctl -u status-led -f
```

> **Note:** For the SPI variant as a normal user, that user must be in the groups `spi` and `i2c` (default on Raspberry Pi OS): `groups YOUR_USER` and `sudo usermod -aG spi,i2c YOUR_USER`.

---

## 11. Cycle the display and reboot (push button)

A push button on **GPIO17 (pin 11)** controls the display and can reboot the Pi:

- **Short press** — advances the display by one page. Page 0 is the familiar 4-line overview (IP, CPU, RAM, status); the following pages each show one value on its own in a **large, auto-fitted font**, in this order: **installed version** (first), IP, **hostname** (right after IP), CPU, RAM, status, **uptime**, **network throughput** (↓ rx / ↑ tx) and — if enabled — **disk temperature** and **fan speed**. After the last page it wraps back to the overview.
- **Auto-return** — after about 30 seconds without a press the display jumps back to the overview (page 0). Configurable via `oled.page_timeout_s`.
- **Long press (≥ 5 s)** — reboots the Pi. The OLED shows “Neustart…” and the LED turns red; this message stays for `button.reboot_message_s` seconds (default 3) before `systemctl reboot` runs. The hold time is set by `button.long_press_s`.

The reboot needs root privileges — with the recommended PWM setup the service already runs as `User=root`, so it works out of the box. For the SPI variant under a normal user you would need a sudoers/polkit rule. The button uses Blinka’s `digitalio` and needs no extra package. Disable it with `--no-button` or `button_enabled = False`.

---

## 12. Automated backup with restic (optional)

The project can set up an unattended **restic** backup to an **SMB network share**, scheduled by a systemd timer. It drives the existing backup states automatically: while a backup runs the LED pulses **cyan** ("Backup laeuft..."), a failure shows **magenta** ("Backup-Fehler!") until the next success.

### Set it up

`install.sh` asks whether to configure a backup. You can also (re)run it any time:

```bash
sudo status-led backup-setup
```

The wizard asks for: **frequency** (daily / weekly / monthly) and **time** (hh:mm), the **SMB share** `//server/share` and its credentials, the **restic repository password**, the **source paths** (default `/home /etc`) and the **retention** (keep daily / weekly / monthly). It then installs `restic` + `cifs-utils`, writes the config, creates the systemd service + timer, and offers to run the **first backup** right away (which initialises the repository).

### Commands

```bash
sudo status-led backup         # run a backup now
sudo status-led backup-setup   # change schedule / target / paths
status-led backup-status       # show the timer and the last run's log
```

### Files it creates

| File | Purpose |
|---|---|
| `/etc/status-led/backup.env` | non-secret settings (target, mountpoint, repo path, sources, retention) |
| `/etc/status-led/restic-password` (0600) | restic repository password |
| `/etc/status-led/smb-credentials` (0600) | SMB username / password |
| `status-led-backup.service` / `.timer` | the scheduled job (`OnCalendar`, `Persistent=true`) |

**How it works:** the runner (`backup-run.sh`) mounts the SMB share on demand, runs `restic backup` (initialising the repo on first run), then `restic forget --prune` for retention, and writes `running`/`ok`/`failed` to `/run/status-led/backup`. The share is unmounted again afterwards.

> **Security:** the restic password and SMB credentials are stored in root-only (0600) files so unattended backups can run. The backup service runs as root (needed to mount the share and read all source paths).

### Custom backup job instead

If you prefer your own backup job, just have it write `running`, `ok` or `failed` to `/run/status-led/backup` — the LED/OLED reflect it:

```bash
#!/bin/bash
mkdir -p /run/status-led
echo running > /run/status-led/backup

if restic backup /data; then
    echo ok > /run/status-led/backup
else
    echo failed > /run/status-led/backup
fi
```

> **Note:** The failure state (LED magenta, OLED "Backup-Fehler!") stays until the file is set back to `ok`.

---

## 13. Reference: states, LED colour and OLED text

| State                 | LED colour & pattern  | OLED text         | Prio | Enabled by default |
|-----------------------|-----------------------|-------------------|------|--------------------|
| Over-temperature      | Red/green, 1 Hz       | UEBERTEMPERATUR!  | 100  | yes                |
| SMART disk failure    | White, 3 Hz blinking  | SMART-Fehler!     | 90   | no (`smart`)       |
| Fan warning           | Orange, 2 Hz blinking | Luefter-Warnung!  | 85   | no (`fan`)         |
| No network            | Blue, 2 Hz blinking   | Kein Netzwerk     | 80   | yes                |
| Backup failed         | Magenta, 2 Hz         | Backup-Fehler!    | 70   | yes                |
| Low disk space        | Yellow, 1 Hz blinking | Speicher voll!    | 60   | yes (`diskspace`)  |
| Backup running        | Cyan, pulsing         | Backup laeuft...  | 40   | yes                |
| High CPU load         | Amber, pulsing        | CPU-Last hoch     | 30   | yes (`cpuload`)    |
| Normal operation      | Green (bright on I/O) | Normalbetrieb     | 0    | yes                |

The SMART and fan states are **off by default** because they need extra software or specific hardware: SMART needs `smartmontools` (`sudo apt install -y smartmontools`) and root. Enable them in the config (`[smart] enabled = true`, `[fan] enabled = true`).

**Fan:** a real RPM tach is read from `/sys/class/hwmon` (e.g. some case fans). The official **PoE/PoE+ HAT** fan is firmware-controlled and has *no* RPM tach — instead it exposes a thermal `cooling_device` with a stage (0…max), which is shown as `St.x/y` on the OLED. With a tach, `fan.warn_below_rpm` triggers a warning below that RPM; with a stage fan, `fan.warn_at_max = true` warns when it sits at the top stage. Otherwise the fan value is display-only.

**SMART:** an SD-card boot has no SMART device (SD cards don't support it). For a USB SSD/HDD the USB bridge often needs `-d sat`, which the probe tries automatically.

> **Tip:** Run `status-led diag` (or `python3 status_led.py --diag`) to see exactly what is detected for OLED, SMART and the fan — the quickest way to find out why a value is missing.

When several states are active, the one with the highest priority wins (for both LED colour and OLED text). The OLED also permanently shows IP, CPU temperature + 1-minute load and RAM.

> **Note:** The OLED texts are defined in `STATUS_TEXT` in `status_led.py` (German by default). Edit them there to localise the display.

---

## 14. Add your own states

Each state consists of a condition and a render function and is registered with a priority in the `STATUSES` list. For the OLED display, also add a text in `STATUS_TEXT`:

```python
def is_my_state(ctx):
    return ...                  # True when active

def render_my_state(ctx):
    return (1.0, 0.5, 0.0)      # colour (R, G, B), each 0..1

STATUSES.append(StatusDef("my_state", priority=50,
                          condition=is_my_state,
                          render=render_my_state))
STATUS_TEXT["my_state"] = "My text"
```

The built-in optional states (`smart`, `fan`, `cpuload`, `diskspace`) follow the same pattern — their condition functions simply return `False` when disabled in the config, so toggling `[smart] enabled = …` etc. is all you need to turn them on or off. When you add a state with its own settings, add the fields to the `Config` dataclass and a mapping entry in `CONFIG_MAP` so they can be set from the TOML file too. The status logic is covered by the tests in `tests/` — add a case there for your new state.

---

## 15. Troubleshooting (from practice)

The following cases come from a real setup — from the service start to the flickering LED.

### ▶ Service does not start, status "status=217/USER" in the log
- **Cause:** The user given in the `.service` file does not exist. Current Raspberry Pi OS no longer has the former default user `pi`.
- **Fix:** For PWM set `User=root`; for SPI use the real username. Then `daemon-reload` and `restart`.

### ▶ pip error "externally-managed-environment"
- **Cause:** On Bookworm the system Python is protected (PEP 668) — or a backslash sits in the middle of the line instead of at the end and breaks the command.
- **Fix:** Put each command on **one** line and add `--break-system-packages` at the end, or use the venv variant (section 6).

### ▶ LED does not light at all, although the script runs without errors
- **Cause:** Wiring (DIN/DOUT swapped, no common ground) or a logic level that is too low (VDD at 5 V, data only 3.3 V).
- **Fix:** First check full brightness with the direct test (section 9). If nothing lights: check the DIN side and GND, then move VDD from 5 V to **3.3 V** as a test. For a single LED, 3.3 V is the permanent solution.

### ▶ Wrong colours (e.g. red commanded, green lights up)
- **Cause:** The colour-channel order of the LED differs. The standard is `GRB`; some batches differ.
- **Fix:** Use the colour test (section 9) to find which order makes "red" actually red, and enter that value in `ws_pixel_order`. GRB is correct most of the time.

### ▶ LED drifts to white when idle, "off" does not stay off (with SPI)
- **Cause:** The Pi leaves the SPI data line (MOSI) High between packets. The WS2812B reads this as all ones = white. Bright colours survive it, dim green and "off" flip.
- **Fix:** Switch to the **PWM method (GPIO18)** — it pulls the line cleanly to Low. Move the data line to pin 12, set `led_type="ws2812"`, disable audio, run the service as root (sections 6–10).

### ▶ Only one of several LEDs shows the status, the rest stay (white)
- **Cause:** `ws_count` is too low — surplus LEDs keep their last value.
- **Fix:** Set `ws_count` to the actual number of LEDs and restart the service.

### ▶ PWM: LED flickers or shows wrong colours despite correct wiring
- **Cause:** The onboard audio is still active and occupies the PWM hardware.
- **Fix:** Enter `dtparam=audio=off` in `/boot/firmware/config.txt` and reboot (section 7).

### ▶ OLED stays dark
- **Cause:** I2C not enabled, wrong address, PIL missing or wiring.
- **Fix:** `sudo i2cdetect -y 1` — does `0x3c` appear? Otherwise enable I2C, install `python3-pil`/pillow, check SDA/SCL/3V3/GND; if needed put `0x3d` in `oled_addr`.

### ▶ Neither LED nor OLED respond; /dev/i2c-1 or /dev/spidev0.0 are missing
- **Cause:** Not a real Raspberry Pi (e.g. LXC container/VM on x86) or the bus is not enabled.
- **Fix:** `cat /proc/device-tree/model` must show a Pi. In an LXC/VM without a passed-through GPIO/I2C/SPI interface there are no pins — then run it on real Pi hardware.

### ▶ After a system update the LED suddenly flickers again (PWM)
- **Cause:** An update reset `/boot/firmware/config.txt`; `dtparam=audio=off` is missing again.
- **Fix:** Add the line again and reboot.

### ▶ Button does nothing
- **Cause:** Wrong pin, the button is not wired against GND, or it is disabled.
- **Fix:** Wire the button between **GPIO17 (pin 11)** and GND, check `button_pin` and `button_enabled = True`. The internal pull-up means pressed = LOW; no resistor needed.

### ▶ Long press does not reboot
- **Cause:** The service does not run as root (e.g. the SPI variant under a normal user).
- **Fix:** Run the service as `User=root` (the PWM default), or add a sudoers/polkit rule allowing `systemctl reboot`.

### ▶ The large single-line pages show tiny text
- **Cause:** No scalable TrueType font was found, so the small bitmap font is used.
- **Fix:** Install the DejaVu fonts: `sudo apt install -y fonts-dejavu-core`.

### ▶ Fan shows `n/a` even though a PoE HAT is installed
- **Cause:** The PoE/PoE+ HAT fan is firmware-controlled. Without the PoE overlay Linux exposes no fan sensor at all (`status-led diag` shows "weder hwmon-Tacho noch cooling_device"). Third-party PoE HATs (e.g. **Waveshare**) regulate the fan with their own onboard circuit and expose no tach/cooling device to the Pi, so the fan is not readable at all.
- **Fix (official HAT only):** add `dtoverlay=rpi-poe` (PoE HAT) or `dtoverlay=rpi-poe-plus` (PoE+ HAT) to `/boot/firmware/config.txt`, reboot, then check `status-led diag` — a `cooling_device` of type `rpi-poe-fan` should appear and the OLED shows the fan stage (`St.x/y`). The `rpi-poe*` overlays are for the official HAT only; on a third-party HAT (Waveshare etc.) there is no readable value — leave `[fan]` disabled (the fan still runs, controlled by the HAT).

### ▶ SMART shows `n/a` / no device found
- **Cause:** Booting from an SD card (SD cards have no SMART), or a USB SSD whose bridge isn't auto-detected by `smartctl --scan`.
- **Fix:** Check with `status-led diag`. For a USB SSD the probe automatically retries with `-d sat`; verify manually with `sudo smartctl -d sat -i /dev/sda`. If even that fails, your USB bridge needs a specific driver (`-d sntjmicron`, `-d sntasmedia`, …).

### ▶ Backup fails to mount the SMB share (`mount error(2)` / `mount error(13)`)
- **`mount error(2) No such file or directory`** — you gave the **full path including subfolders** as the share. CIFS mounts only `//server/share`; subfolders that don't exist yet can't be mounted.
- **`mount error(13) Permission denied`** — wrong credentials, or the path isn't a real share. On **Synology** the share is the **shared-folder name** (e.g. `//10.0.0.5/HausBackup`), *not* the internal path `//10.0.0.5/volume1/HausBackup`.
- **Fix:** Re-run `sudo status-led backup-setup` and enter only `//server/share` as the share; put any subfolders (e.g. `host/restic`) into the **repo subfolder** prompt. For older NAS add `vers=3.0` or `sec=ntlmssp` under "Mount-Optionen". Test manually: `sudo mount -t cifs //server/share /mnt/test -o username=USER` and check `dmesg | tail`.

### Useful diagnostic commands

```bash
cat /proc/device-tree/model            # real Pi?
ls -l /dev/i2c-1 /dev/spidev0.0        # buses present?
sudo i2cdetect -y 1                    # OLED at 0x3c?
groups YOUR_USER                       # in groups spi/i2c?
python3 /usr/local/bin/status_led.py   # (stop the service) errors in the foreground
journalctl -u status-led -e            # service log with errors
```

---

*Tested, working configuration: `led_type = "ws2812"` (PWM, GPIO18), `ws_pixel_order = "GRB"`, `ws_count = 1`, service as `User=root`, onboard audio disabled, LED VDD at 3.3 V.*

---

## 16. Changelog

The format is based on [Keep a Changelog](https://keepachangelog.com/).

### [1.3.1] — 2026-06-28

**Fixed**
- Backup setup now separates the **SMB share** (`//server/share` only, validated) from the **repo subfolder** (any depth), so deep paths and Synology `volumeX/...` mistakes no longer cause `mount error(2)/(13)`. The runner mounts only the share and creates the subfolders. Clearer mount-error hints and a troubleshooting entry.

### [1.3.0] — 2026-06-27

**Added**
- Optional automated **restic backup** to an SMB share, opt-in during install. `status-led backup-setup` asks for frequency (daily/weekly/monthly) + time, target share + credentials, restic password, source paths and retention, then creates a systemd service + timer and can run the first backup immediately.
- New scripts `backup-run.sh` (runner: mount → restic backup → forget/prune → status file) and `setup-backup.sh` (wizard); `status-led` commands `backup`, `backup-setup`, `backup-status`. The backup writes `/run/status-led/backup`, so LED/OLED show the backup state automatically.

### [1.2.5] — 2026-06-27

**Removed**
- Outdated PDF installation guides (`doc/en/`, `doc/de/*.pdf`). The READMEs are the single, maintained documentation.

### [1.2.4] — 2026-06-27

**Added**
- Two new OLED single-value pages in the button stepping: the **installed version** (shown first) and the **hostname** (right after the IP page). The page-0 overview is unchanged (IP/CPU/RAM/status).

### [1.2.3] — 2026-06-27

**Changed**
- `--diag` now explains how to make a PoE/PoE+ HAT fan visible to Linux (load the `rpi-poe`/`rpi-poe-plus` overlay) when no fan sensor is found.
- Troubleshooting: added entries for the PoE-HAT fan showing `n/a` and for SMART on SD-card/USB-SSD setups.

### [1.2.2] — 2026-06-27

**Fixed**
- `update.sh` now also refreshes the `status-led` command in `/usr/local/bin`, so new subcommands (e.g. `diag`) arrive through `status-led update`. Existing installs need one re-run of the installer to pick up this fix.

### [1.2.1] — 2026-06-27

**Added**
- `status_led.py --diag` / `status-led diag`: hardware diagnostics for OLED, SMART and fan — shows what is detected when values are missing.
- Fan support for the official PoE/PoE+ HAT (and other firmware-controlled fans) via the thermal `cooling_device`: shows the fan stage (`St.x/y`) when there is no RPM tach; `fan.warn_at_max` warns at the top stage.

**Changed**
- Button reboot now keeps the "Neustart" message on the OLED for `button.reboot_message_s` seconds (default 3) before rebooting.
- SMART probe is more robust: also tries `-d sat` (USB‑SATA bridges) and falls back to block‑device nodes.

### [1.2.0] — 2026-06-27

**Added**
- One-line installer (`install.sh`) for the Raspberry Pi: installs sources and libraries into a virtual environment, enables I2C/SPI, sets up the systemd service.
- Interactive configuration wizard (`status_led.py --setup`) that writes `config.toml`, including OLED address detection via `i2cdetect`.
- In-place updater (`update.sh` / `status-led update`) that keeps the existing config, plus a `status-led` helper command (setup/update/status/logs/restart).
- `--version` flag and `requirements.txt`.

### [1.1.0] — 2026-06-27

**Added**
- Configuration file in TOML format (`/etc/status-led/config.toml`, override with `--config`) — no need to edit the script, and your settings survive updates. Template: `config.example.toml`.
- New states: SMART disk health (optional), fan warning (optional), low free disk space, high CPU load (with hysteresis).
- Extra OLED pages: uptime, network throughput, disk temperature, fan speed.
- GPIO17 push button: short press cycles the display pages, long press (≥ 5 s) reboots.
- Unit tests for the status logic and the config loader (`python3 -m unittest discover -s tests`).
- MIT license.

### [1.0.0] — 2026-06-27

- Initial release: WS2812B status LED + SSD1306 OLED, driven by a single script and one systemd service.
- States: over-temperature (with hysteresis), no network, backup failed/running, normal operation.
- Documentation in English and German.

---

## 17. License

This project is released under the **MIT License**. You may use, modify and distribute it freely, including commercially, as long as the copyright notice and the license text are retained. The full text is in the [LICENSE](LICENSE) file.

Copyright © 2026 Silvio Sternitzke
