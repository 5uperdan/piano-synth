# Raspberry Pi Piano Sound Module

Turns a Raspberry Pi 4 into a silent, always-on soundfont player. A digital
piano sends MIDI in over USB, FluidSynth renders it, and audio comes out of a
USB sound card. A Sense HAT on top lets you browse and switch soundfonts
without a screen or a keyboard.

Power it on, wait a few seconds, play. Nothing to log into.

## Contents

```
piano-synth/
├── README.md                        this file
├── config.toml                      all tunable settings
├── pyproject.toml                   uv project definition
├── piano_control.py                 the Sense HAT application
├── font3x5.py                       pixel font for scrolling text
└── systemd/
    ├── fluidsynth.service           the audio engine
    └── piano-control.service        the Sense HAT front end
```

Two services, deliberately. FluidSynth owns the audio and never needs to
restart; the control app talks to it over a local TCP socket. If the control
app crashes or you edit its config, audio keeps playing throughout.

## How it behaves

The LED matrix is blank while idle.

Soundfonts are sorted alphabetically and laid out across the grid left to
right, one row at a time: font *i* sits at row `i // 8`, column `i % 8`.
Maximum 64.

| Input | What happens |
|---|---|
| First joystick nudge (any direction) | Wakes up, lights the currently loaded font for 0.5s, then scrolls its name. Cursor does not move. |
| Further nudges | Move the cursor, light the new position, scroll that name. |
| Any input mid-animation | Cancels it immediately and starts the new one. |
| Press joystick in | Loads the font under the cursor: three amber flashes, a steady amber pixel while it loads, two green flashes on success (red on failure). |
| No input for 6 seconds | Display sleeps, so the next nudge is a wake again. |

While browsing, the currently loaded font shows as a dim green pixel so you
can see where you started. The chosen font is remembered and reloaded on next
boot.

---

# Setup

Assumes a Raspberry Pi 4 with Raspberry Pi OS **Lite 64-bit** (Bookworm or
later), SSH working, and the user `danny`. Substitute your own username
throughout if different — it appears in both service files and in
`config.toml`.

## 1. Hardware

- Raspberry Pi 4. **2GB minimum, 4GB recommended** — FluidSynth loads the
  entire soundfont into RAM, so a 600MB soundfont needs 600MB resident. On 2GB,
  set `load_before_unload = false` in `config.toml` (see step 9).
- Sense HAT.
- USB sound card (e.g. Sound Blaster Play!). The Pi's own 3.5mm output is
  PWM-based and noticeably noisy — don't use it for this.
- USB-MIDI interface, connected to the piano's **MIDI OUT** socket only. Do
  not connect the return leg; it does nothing here and can cause MIDI feedback.

**Thermal note:** the Sense HAT sits flat over the SoC and blocks airflow, and
prevents fitting most heatsink cases. A **GPIO stacking header** (a few pounds)
lifts it 15-20mm clear, restores airflow, and leaves room for a low-profile
heatsink underneath. Worth doing. `low_light = true` in the config also keeps
the LED matrix from adding its own heat.

## 2. Boot configuration

Edit `/boot/firmware/config.txt`:

```bash
sudo nano /boot/firmware/config.txt
```

Change the audio line and enable I2C for the Sense HAT:

```ini
# Sense HAT needs I2C
dtparam=i2c_arm=on

# Disable onboard audio so the USB card is unambiguous
dtparam=audio=off

# Suppress HDMI audio devices too
dtoverlay=vc4-kms-v3d,noaudio
```

Disabling onboard and HDMI audio means the USB card is the only sound device,
which removes any chance of ALSA card numbers shuffling between boots.

Reboot: `sudo reboot`

## 3. Install packages

```bash
sudo apt update
sudo apt install -y fluidsynth alsa-utils python3-sense-hat python3-rtimulib \
                    cpufrequtils rfkill git curl
```

`python3-sense-hat` and `python3-rtimulib` must come from apt. RTIMULib is a
C++ library with no working PyPI package — this is the one dependency uv
cannot manage, which is why step 7 uses `--system-site-packages`.

Confirm the Sense HAT is alive:

```bash
python3 -c "from sense_hat import SenseHat; s=SenseHat(); s.show_letter('P'); import time; time.sleep(1); s.clear()"
```

A letter P should appear for a second. If you get an I2C error, check step 2
and that the HAT is seated properly.

## 4. Identify the sound card

With the USB sound card plugged in:

```bash
aplay -L | grep -A1 CARD=
```

Look for the entry naming your device, something like:

```
hw:CARD=Device,DEV=0
    USB Audio Device, USB Audio
    Direct hardware device without any conversion
```

You want the **`hw:CARD=...`** form, not `plughw:` or `default:` — `hw:` is
direct hardware access with no resampling layer, which is what you want for
low latency. Note the exact string; it goes into the service file in step 8.

Test it makes noise:

```bash
speaker-test -D hw:CARD=Device,DEV=0 -c 2 -t sine -l 1
```

## 5. Confirm MIDI arrives

Plug in the USB-MIDI interface and check the Pi sees it:

```bash
aconnect -l
```

You should see a client for your MIDI interface. Then watch for note data:

```bash
aseqdump -p "USB MIDI Interface"
```

Play keys on the piano. You should see note-on and note-off events, alongside
a steady stream of clock and active-sensing messages — that noise is normal and
FluidSynth ignores it.

**If nothing appears,** swap the two DIN plugs at the piano end. These cables
are labelled inconsistently and getting it the wrong way round is the single
most common cause of silence. Ctrl+C to stop.

## 6. Realtime privileges

Without these, the audio thread gets preempted by other processes and you get
crackles and dropouts under load.

```bash
sudo usermod -a -G audio danny
sudo tee /etc/security/limits.d/audio.conf >/dev/null <<'EOF'
@audio - rtprio 90
@audio - nice -10
@audio - memlock unlimited
EOF
```

Log out and back in (group membership only attaches at session start), then
verify:

```bash
groups          # must include 'audio'
ulimit -r       # must print 90, not 0
```

Both service files also set `LimitRTPRIO` and `LimitMEMLOCK` directly, because
systemd services do not read `limits.conf`. Belt and braces — you need both.

## 7. Lock CPU frequency

The Pi scales its clock down when idle and takes a moment to ramp up, which
shows up as dropouts on the first notes after a pause.

```bash
sudo tee /etc/default/cpufrequtils >/dev/null <<'EOF'
GOVERNOR="performance"
EOF
sudo systemctl enable --now cpufrequtils
```

Verify: `cpufreq-info | grep "current policy"` should show the performance
governor. This raises idle power draw and temperature slightly — acceptable
for an always-on appliance, and the reason the stacking header in step 1 is
worth having.

## 8. Install the application

```bash
# Copy this directory to /home/danny/piano-synth, then:
cd /home/danny/piano-synth

# Install uv if you don't have it
curl -LsSf https://astral.sh/uv/install.sh | sh
source ~/.bashrc

# System site packages so the venv can see apt's python3-sense-hat
uv venv --system-site-packages
uv sync
```

Check the venv can reach the Sense HAT library:

```bash
.venv/bin/python -c "import sense_hat; print('sense_hat ok')"
```

If that fails, the venv was created without `--system-site-packages`. Delete
`.venv` and redo it.

## 9. Configure

```bash
mkdir -p /home/danny/soundfonts
nano config.toml
```

Set `soundfont_dir` if you used a different path, and check
`load_before_unload`:

- **4GB Pi:** leave `true`. The new soundfont loads before the old one is
  freed, so switching is quick, at the cost of both being resident briefly.
- **2GB Pi:** set `false`. Halves peak RAM at the cost of a longer gap when
  switching. With a 600MB font on 2GB, `true` risks swapping to the SD card,
  which will destroy audio performance.

If the Sense HAT is mounted rotated relative to where you sit, set `rotation`
(LED orientation) and `joystick_rotation` (direction remapping) to match.

## 10. Add soundfonts

```bash
cd /home/danny/soundfonts
curl -L -o Yamaha-C5-Salamander.sf2 'URL_HERE'
```

Only `.sf2` and `.sf3` are picked up. Filenames become the scrolling display
names, so keep them short and descriptive — `Yamaha-C5` reads better on an
8x8 grid than `Yamaha-C5-Salamander-JNv4.0-final`. The font covers A-Z, 0-9
and common punctuation; anything else renders as `?`.

The directory is scanned once at startup. After adding files:

```bash
sudo systemctl restart piano-control
```

## 11. Install the services

Edit `systemd/fluidsynth.service` and replace `hw:CARD=Device,DEV=0` with the
exact string from step 4. Then:

```bash
sudo cp systemd/fluidsynth.service systemd/piano-control.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now fluidsynth.service
sudo systemctl enable --now piano-control.service
```

Check both:

```bash
systemctl status fluidsynth.service piano-control.service
journalctl -u piano-control -f
```

You should see the soundfont count, a connection message, and a load
confirmation. The matrix does a brief green sweep when ready, then blanks.

Reboot to confirm it comes up clean, then play.

---

# Latency tuning

Buffer settings live in `systemd/fluidsynth.service`:

```
-o audio.period-size=128
-o audio.periods=2
```

At 48kHz, `period-size=128` with 2 periods is roughly 5.3ms of buffer. Start
here. Once it's stable, try 64 — halving it again — and back off if you hear
crackling. Higher values (256, 512) are safer but you'll feel the delay.

After each change: `sudo systemctl restart fluidsynth`

Other things that matter, in rough order of impact:

1. **Realtime privileges** (step 6). Biggest single factor. If `ulimit -r`
   returns 0, nothing else will help.
2. **CPU governor** (step 7).
3. **Running Lite with no desktop.** Nothing else competing for CPU.
4. **Sample rate.** `synth.sample-rate=48000` matches what most USB sound
   cards run natively. If yours is 44.1kHz-only, change it to match — a
   mismatch forces resampling somewhere.

Watch for underruns while playing:

```bash
journalctl -u fluidsynth -f
```

## Is the WiFi toggle worth it?

Modartt warn that WiFi and ethernet drivers can cause CPU spikes that produce
audible pops. That advice is aimed at Pianoteq, which is far heavier than
FluidSynth. At `period-size=128` you have several milliseconds of headroom per
period and WiFi interrupt handling takes microseconds — **the honest
expectation is that you will not hear a difference.**

The toggle is implemented but **disabled by default**. Test it properly before
deciding: play something dense with WiFi up, then with it down, and see whether
you can actually tell. If you can't, leave it off — it's a feature that can
lock you out of your own Pi.

To enable it, set `toggle_enabled = true` in `config.toml` and grant the app
permission to run `rfkill`:

```bash
sudo tee /etc/sudoers.d/piano-wifi >/dev/null <<'EOF'
danny ALL=(root) NOPASSWD: /usr/sbin/rfkill block wifi, /usr/sbin/rfkill unblock wifi
EOF
sudo chmod 440 /etc/sudoers.d/piano-wifi
```

Hold the joystick up for two seconds to toggle. The whole matrix flashes blue
(on) or amber (off) for a moment.

**Safety net:** the app always unblocks WiFi at startup, so if you disable it
and lose SSH, a power cycle brings it back.

---

# Troubleshooting

**No sound at all, but MIDI arrives.**
Check FluidSynth actually opened the audio device:
`journalctl -u fluidsynth -n 50`. An ALSA "device busy" error means something
else has the card — usually a leftover manual `fluidsynth` process. Very small
buffer values can also make ALSA refuse to open the device without a loud
error; try `period-size=256`.

**Sound works but is very quiet.**
Raise `-g 2.0` in the service file (up to about 5.0 before clipping) and check
the card's own mixer with `alsamixer -c 0` — press F5 to see capture and
playback controls, and make sure nothing is muted.

**Crackling or dropouts.**
In order: confirm `ulimit -r` is 90; confirm the performance governor is
active; raise `period-size` to 256; check `journalctl -u fluidsynth` for
underrun messages.

**Sense HAT shows nothing.**
`systemctl status piano-control`. If it's restarting in a loop,
`journalctl -u piano-control -n 50` will say why — usually the venv can't
import `sense_hat` (redo step 8) or it can't reach FluidSynth on port 9800.

**Joystick directions are wrong.**
Set `joystick_rotation` in `config.toml` to 90, 180 or 270 until they match.
This is separate from `rotation`, which only affects the LED matrix.

**Font name scrolls but loading does nothing.**
`journalctl -u piano-control -f` while you press. A load timeout on a large
soundfont from a slow SD card is the usual cause — raise `load_timeout`.

**Switching fonts kills audio / the Pi hangs.**
Out of RAM. Set `load_before_unload = false`, or use smaller soundfonts.
`free -h` while a font is loaded shows how close you are.

**New soundfonts don't appear.**
The directory is only scanned at startup: `sudo systemctl restart
piano-control`. Also check the extension is `.sf2` or `.sf3` and the file is
readable by `danny`.

## Useful commands

```bash
# Talk to FluidSynth directly while it's running
telnet 127.0.0.1 9800        # then: fonts, channels, gain 3, help

# Watch both services
journalctl -u fluidsynth -u piano-control -f

# Run the control app by hand (stop the service first)
sudo systemctl stop piano-control
PIANO_LOG_LEVEL=DEBUG .venv/bin/python piano_control.py
```

## A note on the FluidSynth port

FluidSynth's command shell on port 9800 is an unauthenticated remote control
for the synth. On a home network behind a router this is a low risk, but if
you'd rather close it off:

```bash
sudo apt install -y ufw
sudo ufw allow ssh
sudo ufw deny 9800
sudo ufw enable
```

Local connections from the control app are unaffected.
