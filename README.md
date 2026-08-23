# Raspberry Pi Piano Sound Module

Turns a Raspberry Pi 4 into a silent, always-on soundfont player. A digital
piano sends MIDI in over USB, FluidSynth renders it, and audio comes out of a
USB sound card. A Sense HAT on top lets you browse and switch soundfonts
without a screen or a keyboard.

Everything you play is also kept in a rolling buffer in memory, so if you
stumble onto something worth keeping you can save the last hour to a MIDI file
by holding the joystick in.

Power it on, wait a few seconds, play. Nothing to log into.

## Contents

```
piano-synth/
├── README.md                        this file
├── config.toml                      all tunable settings
├── pyproject.toml                   uv project definition
├── piano_control.py                 the Sense HAT application
├── midi_capture.py                  the rolling MIDI recorder
├── font3x5.py                       pixel font for scrolling text
├── tests/                           run these on any machine, no Pi needed
└── systemd/
    ├── fluidsynth.service           the audio engine
    ├── piano-control.service        the Sense HAT front end
    └── piano-capture.service        the MIDI recorder
```

Three services, deliberately kept apart:

- **fluidsynth** owns the audio and never needs to restart.
- **piano-control** drives the Sense HAT and talks to FluidSynth over a local
  TCP socket. Crash it, or edit its config, and audio keeps playing throughout.
- **piano-capture** records MIDI. It subscribes to the same ALSA sequencer port
  FluidSynth listens to, *in parallel* rather than in line, so it sits beside
  the audio path rather than inside it.

That last point is the whole reason recording is safe to leave on. The kernel
delivers each MIDI event to both subscribers independently: if capture stalls,
its own buffer overflows and it loses its own events, while FluidSynth is
untouched. It also explains why capture is a separate process rather than part
of `piano_control.py` — that program blocks for several seconds while a
soundfont loads, which would punch a hole in the recording every time you
changed sound.

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
| **Hold joystick in (1.5s)** | Saves everything in the recording buffer to a timestamped `.mid`. Whole matrix flashes amber to confirm the hold registered, then green on success or red on failure. Works with the display asleep, and does not wake it. |
| No input for 6 seconds | Display sleeps, so the next nudge is a wake again. |

Because holding and tapping mean different things, the middle button acts when
you **release** it rather than when you press it. For a quick tap the
difference is imperceptible.

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
  set `load_before_unload = false` in `config.toml` (see step 9). The recording
  buffer adds at most ~27MB on top, which is irrelevant on any of these.
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
                    python3-mido python3-rtmidi \
                    cpufrequtils rfkill git curl
```

`python3-sense-hat` and `python3-rtimulib` must come from apt. RTIMULib is a
C++ library with no working PyPI package — this is the dependency uv cannot
manage, which is why step 8 uses `--system-site-packages`.

`python3-rtmidi` is the same story: a C++ binding that wants compiling, so apt
is far less trouble than pip on ARM. `python3-mido` sits on top of it and also
writes the Standard MIDI Files. Both are only needed for recording — if you
set `enabled = false` under `[capture]` you can skip them.

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
low latency. Note the exact string; it goes into the service file in step 11.

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

## 7. CPU frequency and swap

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

### Check what kind of swap you have

```bash
swapon --show
```

This matters because swapping is one of the few things that can stall audio
badly enough to hear. FluidSynth holds the whole soundfont resident, and
faulting a page back from slow storage mid-note is exactly the kind of pause
that causes a dropout.

- **`/dev/zram0`, type `partition`** — the Raspberry Pi OS default, and
  perfectly fine. zram is *compressed swap held in RAM*: no SD card involved,
  no wear, no I/O stall. The worst it can cost is a little CPU to decompress a
  page, and on a Pi with headroom it will never activate at all. **Leave it
  alone.**
- **`/var/swap`, type `file`** — a real file on the SD card, and worth removing
  on an audio box:

  ```bash
  sudo dphys-swapfile swapoff && sudo systemctl disable --now dphys-swapfile
  ```

Either way, `fluidsynth.service` and `piano-capture.service` both set
`MemorySwapMax=0`, which is a cgroup-level guarantee that those two processes
stay resident regardless of what swap exists system-wide. Free insurance
rather than something load-bearing.

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
`load_before_unload`. This setting is **purely about ordering** — the old font
is unloaded either way. What changes is when:

- **4GB or 8GB Pi:** leave `true`. The new font loads and the channels are
  pointed at it *before* the old one is freed, so there is never a moment where
  pressing a key gives you silence. Costs having both resident briefly.
- **2GB Pi:** set `false`. Halves peak RAM, at the cost of a gap of several
  seconds during a switch where the keyboard makes no sound at all. With a
  600MB font on 2GB, `true` risks swapping, which will destroy audio
  performance.

Check with `free -h` if you are unsure which you have.

If the Sense HAT is mounted rotated relative to where you sit, set `rotation`
(LED orientation) and `joystick_rotation` (direction remapping) to match.

The `[capture]` section controls recording. The defaults are sensible; the two
worth a thought are `window_minutes` (how much playing a saved file contains)
and `retention_days` (recordings older than this are deleted automatically
after each save). Set `retention_days = 0` to never delete anything, and
`enabled = false` to turn recording off entirely. See
[MIDI recording](#midi-recording) below.

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
sudo cp systemd/*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now fluidsynth.service
sudo systemctl enable --now piano-capture.service
sudo systemctl enable --now piano-control.service
```

Check all three:

```bash
systemctl status fluidsynth.service piano-capture.service piano-control.service
journalctl -u piano-control -f
```

You should see the soundfont count, a connection message, and a load
confirmation. The matrix does a brief green sweep when ready, then blanks.

Reboot to confirm it comes up clean, then play.

---

# MIDI recording

Everything the piano sends is kept in a ring buffer in memory. Nothing is
written to disk until you ask for it.

**To save:** hold the joystick in for about 1.5 seconds. The whole matrix
flashes amber to confirm the hold registered, then green when the file is
written (red if something went wrong). It works with the display asleep and
does not wake it, because you will be mid-playing and not looking at the HAT.

Files land in `/home/danny/recordings`, named by the moment you saved:

```
2026-08-23_14-32-07.mid
```

Saving is **non-destructive** — the buffer is not cleared, so holding twice
gives you two overlapping files. Seconds are in the filename precisely because
of that.

### What is and isn't in the file

Kept: notes, velocity, **sustain pedal (CC64)**, pitch bend, program changes,
aftertouch. Real elapsed time is encoded at 480 ticks per beat against a 120bpm
tempo, so a saved file plays back at the speed you performed it whatever tempo
your DAW displays.

Dropped at the point of capture: MIDI clock and active sensing. The P-95 emits
a steady stream of both, they carry no musical information, and left in they
would more than double the size of the buffer.

**The soundfont is not recorded anywhere, deliberately.** Capture happens
upstream of FluidSynth, so the bytes are identical whichever font is loaded —
and a `.mid` contains no audio, so the font that happened to be playing at the
time tells you nothing about the file.

### Retention

After each successful save, any `.mid` in the recordings directory older than
`retention_days` (default 30) is deleted. This is deliberately narrow: only
`*.mid`, only regular files, only the top level of that one directory. Set
`retention_days = 0` to disable it and keep everything forever.

If you want to keep something permanently, move it out of that directory.

### Getting recordings off the Pi

```bash
scp danny@pi4Bsynth:recordings/\*.mid ~/Music/piano/
```

### Does it affect audio latency?

It should not, and the reason is structural rather than careful coding.

Capture subscribes to the ALSA sequencer port *in parallel* with FluidSynth.
The kernel delivers each event to both clients independently, writing into each
one's FIFO and returning without waiting for either to consume anything. The
marginal cost to FluidSynth of a second subscriber existing is on the order of
a microsecond per event, against a 5.3ms audio buffer.

The failure mode is contained too: if capture stalls, its own FIFO fills and
the kernel drops events *for that client only*. It cannot stall audio. On top
of that, FluidSynth's audio thread runs `SCHED_FIFO` at realtime priority 90
and capture runs at normal priority with `Nice=10`, so it cannot preempt the
audio thread even in principle. And because saving is manual, nothing touches
the SD card while you play.

**Test it rather than take my word for it**, the same way you would the WiFi
toggle:

```bash
sudo systemctl stop piano-capture
```

Play something dense, watch `journalctl -u fluidsynth -f` for underruns, start
it again, play the same thing. If you can measure a difference, `CPUAffinity`
in the service file can pin capture to a core.

### Tests

The buffer, the file format, the retention sweep and the joystick gesture all
have tests that run on any machine — no Pi, no Sense HAT, no MIDI hardware:

```bash
uv run --with mido --with pytest pytest tests/ -q
```

---

# Latency tuning

Buffer settings live in `systemd/fluidsynth.service`:

```
-o audio.period-size=128
-o audio.periods=2
```

### What that buffer actually is

Your sound card consumes samples at exactly 48,000 a second and never pauses.
FluidSynth computes those samples in software, on an OS that makes no promise
about when any given program gets to run. So FluidSynth works *ahead*: it
renders a chunk of samples and hands it over, and the card plays from that
chunk while the next one is being filled. As long as the next chunk is ready
before the current one drains, output is continuous. A gap would be an audible
click.

`period-size=128` means chunks of 128 samples. At 48kHz that is 128 ÷ 48,000 =
**2.67ms** of audio each. `periods=2` means two of them exist — one playing,
one filling. Total buffer: **5.33ms**.

That reservoir *is* your latency. Press a key and its samples go into the chunk
currently being filled, not the one playing, so the note waits for the current
chunk to drain first.

Smaller period, less delay, less margin: at 128, FluidSynth has 2.67ms to
compute 2.67ms of audio, and every deadline it misses leaves the card with
nothing to play. That is an **underrun**, and it is the crackle you hear.
Everything below exists to make FluidSynth reliably hit that deadline.

### How much to chase

Start at 128. Once it's stable you can try 64, and back off if you hear
crackling. Higher values (256, 512) are safer but you'll feel the delay.

Before spending an evening on it, though, the audio buffer is only one term:

| Stage | Roughly |
|---|---|
| Keybed scan → MIDI emitted by the P-95 | 1–3ms |
| 3-byte note-on over DIN at 31,250 baud | ~1ms |
| USB-MIDI interface (USB frames are 1ms) | ~1ms |
| ALSA sequencer → FluidSynth | microseconds |
| **Audio buffer** | **5.3ms** |
| USB audio out to the sound card | 1–2ms |

Realistically 10–15ms end to end, of which about a third is the part you can
tune. Halving `period-size` saves 2.7ms off a chain where your USB sound card
is quietly contributing a similar amount you cannot touch. Get it stable at 128
and leave it alone unless you can actually feel a lag.

For reference: sound travels ~343 m/s, so 5.3ms is about 1.8 metres of air.
Sitting at a real piano your ear is already a metre or so from the strings.
Under ~10ms is generally imperceptible when playing.

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

**Holding the joystick doesn't save anything.**
`journalctl -u piano-capture -n 50`. Most likely the service can't find a MIDI
port — check `aconnect -l` shows your interface and that `port_match` in
`config.toml` matches part of its name. "Midi Through" is always skipped; it's
ALSA's virtual loopback, not your piano.

**Saving flashes red.**
The control app couldn't reach the capture service, or the buffer was empty.
`systemctl status piano-capture` first. If it's running, check the socket
exists: `ls -l /run/piano/capture.sock`. An empty buffer is normal if you
haven't played anything since the service last started.

**Recordings play back at the wrong speed or sound rhythmically wrong.**
Check the file has a tempo event: `python3 -c "import mido,sys;
print([m for m in mido.MidiFile(sys.argv[1]).tracks[0] if m.is_meta])" file.mid`.
If notes are there but the pedal isn't, something is filtering CC64 — capture
keeps it deliberately.

**Recordings directory is growing.**
Pruning only runs when you save. If you stopped saving but old files remain,
run one save to trigger a sweep, or delete by hand. Check `retention_days`
isn't set to 0.

**New soundfonts don't appear.**
The directory is only scanned at startup: `sudo systemctl restart
piano-control`. Also check the extension is `.sf2` or `.sf3` and the file is
readable by `danny`.

## Useful commands

```bash
# Talk to FluidSynth directly while it's running
telnet 127.0.0.1 9800        # then: fonts, channels, gain 3, help

# Watch all three services
journalctl -u fluidsynth -u piano-control -u piano-capture -f

# Run the control app by hand (stop the service first)
sudo systemctl stop piano-control
PIANO_LOG_LEVEL=DEBUG .venv/bin/python piano_control.py

# How many events are in the recording buffer right now
echo status | socat - UNIX-CONNECT:/run/piano/capture.sock

# Trigger a save without touching the joystick
echo save | socat - UNIX-CONNECT:/run/piano/capture.sock

# Watch raw MIDI arriving, independently of either service
aseqdump -p "USB MIDI Interface"
```

(`socat` is not installed by default: `sudo apt install socat`.)

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
