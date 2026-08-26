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
├── LICENSE                          MIT
├── config.toml                      all tunable settings
├── audio.env.example                template for your sound card name
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
| **Hold joystick down (5s)** | Shuts the Pi down. The grid fills red as you hold; let go at any point and nothing happens. |
| No input for 6 seconds | Display sleeps, so the next nudge is a wake again. |

Because holding and tapping mean different things, the middle button acts when
you **release** it rather than when you press it. For a quick tap the
difference is imperceptible.

While browsing, the currently loaded font shows as a dim green pixel so you
can see where you started. The chosen font is remembered and reloaded on next
boot.

---

# Setup

Twelve steps, start to finish. Allow about an hour, most of it downloads.

**Before you start you need:** a Raspberry Pi 4 running Raspberry Pi OS
**Lite 64-bit** with SSH working, and a shell on it. Verified end to end on
Debian 13 (trixie), kernel 6.18, Python 3.13.

Every command below is run **on the Pi**, from an SSH session, and uses
`$USER` so it works whatever your username is. Check yours resolves:

```bash
echo $USER
```

---

## 1. Hardware

| Part | Notes |
|---|---|
| Raspberry Pi 4 | 2GB minimum, 4GB+ recommended. FluidSynth holds the whole soundfont in RAM, so a 600MB font needs 600MB resident. Recording adds ~11MB on top for a busy hour. |
| Sense HAT | The control surface: joystick to browse, LED matrix to display. |
| USB sound card | e.g. Sound Blaster Play!. The Pi's own 3.5mm output is PWM-based and noticeably noisy — don't use it. |
| USB-MIDI interface | Connected to the piano's **MIDI OUT** only. |
| GPIO stacking header | Optional but recommended — see below. |

**Only connect the piano's MIDI OUT.** The return leg does nothing here (the
whole point is to bypass the piano's own sound engine) and it can cause MIDI
feedback — doubled or stuck notes.

**Cable labelling gotcha:** budget USB-MIDI cables label their DIN plugs by the
socket they belong in, but the convention is applied inconsistently. If no note
data appears in step 5, swap the plugs before assuming anything is broken.

**Thermal note:** the Sense HAT sits flat over the SoC, blocks airflow, and
prevents fitting most heatsink cases. A **GPIO stacking header** (a few pounds)
lifts it 15–20mm clear, restores airflow, and leaves room for a low-profile
heatsink underneath. `low_light = true` in the config also keeps the LED matrix
from adding its own heat.

---

## 2. Boot configuration

Enable I2C for the Sense HAT and disable every audio device except the USB
card, so ALSA card numbers can never shuffle between boots.

```bash
sudo nano /boot/firmware/config.txt
```

Set these three lines:

```ini
dtparam=i2c_arm=on
dtparam=audio=off
dtoverlay=vc4-kms-v3d,noaudio
```

**Then enable the `i2c-dev` module.** This step is easy to miss: `dtparam=i2c_arm=on`
brings up the I2C *bus*, but the `/dev/i2c-*` character devices that the
`sense_hat` library actually talks through come from a separate kernel module.
Enabling I2C via `raspi-config` would do both; editing `config.txt` by hand
does only the first, and you get a Sense HAT that is visible on the bus but
unreachable from Python.

```bash
echo i2c-dev | sudo tee /etc/modules-load.d/i2c-dev.conf
```

(The older advice is `echo i2c-dev >> /etc/modules`. On Trixie that file now
announces itself as obsolete, so use `modules-load.d` instead.)

```bash
sudo reboot
```

---

## 3. Install packages

```bash
sudo apt update
sudo apt install -y fluidsynth alsa-utils python3-sense-hat python3-rtimulib \
                    python3-mido python3-rtmidi \
                    rfkill git curl socat
```

**Immediately disable the FluidSynth service Debian ships.** The package
installs its own *user-level* unit, enabled globally, which starts a generic
General MIDI synth on login. It takes exclusive ownership of the USB sound
card **and** binds TCP port 9800 — which is FluidSynth's default shell port,
and therefore exactly the one this project uses. Leave it in place and our
`fluidsynth.service` can never start, with an error that is far from obvious
when read through systemd.

```bash
systemctl --user disable --now fluidsynth.service
sudo systemctl --global disable fluidsynth.service
```

Both lines are needed. The first stops it now; the second removes the global
symlink under `/etc/systemd/user/`, without which it returns at next login.

Confirm nothing is holding the port or the card:

```bash
pgrep -a fluidsynth        # should print nothing
```

Three of these must come from apt rather than pip: `python3-rtimulib` (which
`python3-sense-hat` needs) and `python3-rtmidi` are C/C++ libraries that either
have no working PyPI package or want compiling on ARM. This is why step 8
creates the virtualenv with `--system-site-packages`.

`python3-mido` and `python3-rtmidi` are only needed for recording. Skip them if
you set `enabled = false` under `[capture]` in step 9.

**Check:** the Sense HAT is alive.

```bash
python3 -c "from sense_hat import SenseHat; s=SenseHat(); s.show_letter('P'); import time; time.sleep(1); s.clear()"
```

A letter P should appear for a second. An I2C error means step 2 didn't take,
or the HAT isn't seated properly.

---

## 4. Identify the sound card

With the USB sound card plugged in:

```bash
aplay -L | grep -A1 CARD=
```

Look for your device:

```
hw:CARD=Device,DEV=0
    USB Audio Device, USB Audio
```

Use the **`hw:`** form, not `plughw:` or `default:` — `hw:` is direct hardware
access with no resampling layer in the way.

**Record it now**, in `audio.env`:

```bash
cd ~/piano-synth
cp audio.env.example audio.env
nano audio.env          # set ALSA_DEVICE to your string
```

This is the only setting that differs between machines, which is why it lives
in a gitignored file rather than in the tracked service unit. Keeping the unit
identical everywhere means `git pull` never collides with a local edit.

**Check:** it makes noise.

```bash
speaker-test -D hw:CARD=Device,DEV=0 -c 2 -t sine -l 1
```

---

## 5. Confirm MIDI arrives

```bash
aconnect -l
```

You should see a client for your MIDI interface. Note its name — you may need
it for `port_match` in step 9.

**Check:** note data actually flows.

```bash
aseqdump -p "USB MIDI Interface"
```

Play some keys. You should see note-on and note-off events, alongside a steady
stream of clock and active-sensing messages — that noise is normal, and both
FluidSynth and the recorder ignore it. Ctrl+C to stop.

**Nothing appears?** Swap the two DIN plugs at the piano end. That is by far
the most common cause of silence.

---

## 6. Realtime privileges

Without these the audio thread gets preempted by other processes and you get
crackles and dropouts under load. This is the single biggest factor in whether
the whole thing sounds clean.

```bash
sudo usermod -a -G audio $USER
sudo tee /etc/security/limits.d/audio.conf >/dev/null <<'EOF'
@audio - rtprio 90
@audio - nice -10
@audio - memlock unlimited
EOF
```

**Log out and back in** — group membership only attaches at session start.

**Check:**

```bash
groups          # must include 'audio'
ulimit -r       # must print 90, not 0
```

If `ulimit -r` prints 0, nothing else in this guide will make it sound good.
Fix this before continuing.

The service files also set `LimitRTPRIO` and `LimitMEMLOCK` directly, because
systemd services do not read `limits.conf`. You need both.

---

## 7. CPU frequency and swap

The Pi clocks down when idle and takes a moment to ramp up, which can show up
as dropouts on the first notes after a pause.

**Do this one.** An earlier version of this README called it optional, on the
grounds that idle load sits around 0.08 and nothing crackles during ordinary
playing. That was measured too gently: dense chords with the sustain pedal down
crackle audibly on `ondemand`, and instrumenting it showed exactly why.

Sampling FluidSynth's CPU and the core clocks four times a second during dense
playing, **27 samples caught it working at 24–40% of a core while all four
cores sat at 600–700 MHz.** The load is bursty and spread across three render
threads, so per-core utilisation looks like ~10% and `ondemand` never crosses
its ramp-up threshold. At 600 MHz you have a third of the compute per period —
a chord that renders in 1ms at full clock needs 3ms, against a 2.67ms deadline.
That miss is the crackle.

With the governor pinned, the same measurement gave **zero** samples below
1000 MHz and zero busy-and-downclocked moments, and peak usage fell from 39.7%
to 35.9% because identical work occupies less core-time at three times the
clock. Temperature rose only 46°C to 48°C.

> **`cpufrequtils` does not exist on Trixie** (`apt-cache policy` reports no
> candidate). Older versions of this README told you to install it; that
> instruction fails on Debian 13.

If you do need it, a small unit avoids depending on any package:

```bash
sudo tee /etc/systemd/system/cpu-performance.service >/dev/null <<'EOF'
[Unit]
Description=Pin CPU governor to performance for low-latency audio

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/sh -c 'for c in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do echo performance > "$c"; done'

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl enable --now cpu-performance.service
```

**Check:** `cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor` prints
`performance`. This raises idle power and temperature slightly — acceptable for
an always-on appliance, and part of why the stacking header in step 1 is worth
having.

### Then check what kind of swap you have

```bash
swapon --show
```

Swapping is one of the few things that can stall audio badly enough to hear:
FluidSynth holds the whole soundfont resident, and faulting a page back from
slow storage mid-note causes a dropout.

- **`/dev/zram0`, type `partition`** — the Raspberry Pi OS default, and fine.
  zram is *compressed swap held in RAM*: no SD card involved, no wear, no I/O
  stall. On a Pi with headroom it will never activate at all. **Leave it.**
- **`/var/swap`, type `file`** — a real file on the SD card. Worth removing:

  ```bash
  sudo dphys-swapfile swapoff && sudo systemctl disable --now dphys-swapfile
  ```

Either way `fluidsynth.service` and `piano-capture.service` both set
`MemorySwapMax=0`, a cgroup-level guarantee that those two stay resident
whatever swap exists system-wide.

---

## 8. Install the application

```bash
git clone https://github.com/5uperdan/piano-synth.git /home/$USER/piano-synth
cd /home/$USER/piano-synth
```

Create the virtualenv. **`--system-site-packages` is not optional** — it is how
the venv sees the apt-installed `sense_hat`, `rtmidi` and `mido`:

```bash
python3 -m venv --system-site-packages .venv
```

> **Why not uv?** Earlier versions of this README used it. But
> `pyproject.toml` declares `dependencies = []` — every real dependency comes
> from apt, because all three are C-backed libraries that don't install
> reliably from PyPI on ARM. That leaves uv with nothing to manage beyond
> creating an empty venv, which Python does natively. It also isn't packaged
> for Trixie, so using it means running an install script off the internet for
> no benefit. If you want uv anyway, `uv venv --system-site-packages` is a
> drop-in replacement for the line above.

**Check:** the venv can reach the system libraries.

```bash
.venv/bin/python -c "import sense_hat, rtmidi, mido; print('all imports ok')"
```

If that fails, the venv was created without `--system-site-packages`. Delete
`.venv` and redo it.

---

## 9. Configure

```bash
mkdir -p ~/soundfonts ~/recordings
nano config.toml
```

Paths in `config.toml` use `~`, which the application expands, so there is
nothing to substitute here regardless of your username.

**`load_before_unload`** is purely about *ordering* — the old font is unloaded
either way. What changes is when:

- **4GB or 8GB Pi:** leave `true`. The new font loads and channels are pointed
  at it *before* the old one is freed, so there is never a moment where a key
  gives you silence. Costs having both resident briefly.
- **2GB Pi:** set `false`. Halves peak RAM, at the cost of several seconds of
  total silence during a switch.

Check which you have with `free -h`.

**`[capture]`** controls recording. The defaults are sensible; the two worth a
thought are `window_minutes` (how much playing a saved file contains) and
`retention_days` (recordings older than this are deleted automatically after
each save — set `0` to keep everything forever). Set `enabled = false` to turn
recording off entirely. If step 5 showed a port name unlike "USB MIDI
Interface", set `port_match` to a distinctive part of it.

**Sense HAT mounted rotated?** Set `rotation` (LED orientation) and
`joystick_rotation` (direction remapping) to match how you sit.

---

## 10. Add soundfonts

**You already have one.** Installing `fluidsynth` in step 3 pulls in
`fluid-soundfont-gm`, so there is a 142MB General MIDI font on disk before you
download anything. Its grand piano is ordinary but perfectly good for proving
the system works — link it in and you can skip ahead:

```bash
ln -s /usr/share/sounds/sf2/FluidR3_GM.sf2 ~/soundfonts/FluidR3-GM.sf2
ln -s /usr/share/sounds/sf2/TimGM6mb.sf2   ~/soundfonts/TimGM6mb.sf2
```

Two fonts rather than one is worth it during setup: it lets you test cursor
movement and font switching, not just loading. Symlinks are fine — the scanner
follows them.

### Getting a better piano

The GM font is fine for proving things work, but its grand piano is exactly as
ordinary as you'd expect from a general-purpose bank. For something that
actually sounds like a piano, the **soundfonts4u** collection on Hugging Face
is the easiest source — direct download, no account, no sign-up page in the
way:

```bash
cd ~/soundfonts
curl -L --fail -o Nice-Steinway.sf2 \
  'https://huggingface.co/datasets/projectlosangeles/soundfonts4u/resolve/main/Nice-Steinway-v3.8.sf2'
```

That one is 205MB. Browse the rest at
<https://huggingface.co/datasets/projectlosangeles/soundfonts4u> — any file
there can be fetched by putting its name after `resolve/main/`.

Three things worth knowing:

- **Use `--fail`.** Without it, a 404 or an expired link leaves curl happily
  writing the *error page* into `Nice-Steinway.sf2`. You then get an HTML
  document with a `.sf2` extension, which the scanner picks up and FluidSynth
  rejects with a thoroughly unhelpful message. `--fail` makes curl exit
  non-zero and write nothing.
- **Add `-C -` if your wifi is flaky.** A 205MB download to a Pi is long enough
  to be worth resuming rather than restarting.
- **Rename to something short.** The filename becomes the scrolling display
  name, and `Nice-Steinway` reads far better on an 8x8 grid than
  `Nice-Steinway-v3.8`.

A font this size takes a few seconds to load off the SD card and stays resident
in RAM while selected, which is what `load_before_unload` in step 9 is about.
Licensing across the collection is mixed and not always stated; fine for
playing at home, worth checking before you use one on a recording you publish.

Only `.sf2` and `.sf3` are picked up, and at most 64 (one per LED). Filenames
become the scrolling display names, so keep them short — `Yamaha-C5` reads far
better on an 8x8 grid than `Yamaha-C5-Salamander-JNv4.0-final`. The font covers
A–Z, 0–9 and common punctuation; anything else renders as `?`.

### Adding fonts later

> **The soundfont directory is scanned once, at startup.** Anything you add
> while the service is running is invisible until you restart it:
>
> ```bash
> sudo systemctl restart piano-control
> ```

### Size, and why it matters

Measured read throughput on a Pi 4's SD card is around **43 MB/s**, and
FluidSynth reads the whole file into RAM before it can play a note. That sets
the load time, and the load blocks the control app:

| Soundfont | Size | Cold load |
|---|---|---|
| TimGM6mb | 5.7MB | instant |
| FluidR3-GM | 142MB | ~3s |
| Nice-Steinway | 205MB | ~5s |
| Nice-Keys-Ultimate | 1.2GB | ~30s |

Two consequences worth planning around:

- **Selecting a large font blocks the display.** You get a steady amber pixel
  and an unresponsive joystick until it finishes. Nothing is broken and queued
  input is drained afterwards, but 30 seconds is a long stare at one LED.
- **It also delays boot**, because `state.json` reloads whatever you last
  selected. If a 1.2GB font is your remembered choice, "power on and play"
  becomes "power on, wait half a minute, play". Something smaller as your
  everyday default, with the big one as a deliberate choice, keeps the
  appliance feel.

Repeat loads of the same font are much faster than the table suggests — Linux
keeps it in the page cache, so a 1.2GB font that took 30s cold may reload in
about 5s. The cold figure is the one that applies after a reboot.

Recording is unaffected throughout. `piano-capture` is a separate process, so a
30-second blocking load in `piano-control` doesn't punch a hole in what you
were playing.

---

## 11. Install the services

Check `audio.env` exists from step 4 — the FluidSynth unit reads the card name
from it and will refuse to start without it:

```bash
cd /home/$USER/piano-synth
cat audio.env
```

The unit files carry a literal `$USER` placeholder, because systemd does no
variable expansion of its own. Substitute it as you install them:

```bash
for unit in systemd/*.service; do
  sed "s|[$]USER|$USER|g" "$unit" \
    | sudo tee /etc/systemd/system/"$(basename "$unit")" >/dev/null
done
```

**Check the substitution worked** before starting anything:

```bash
grep -h 'User=\|ExecStart=' /etc/systemd/system/piano-*.service
```

Every path should name your actual home directory, with no `$USER` left.

Then start them, audio engine first:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now fluidsynth.service
sudo systemctl enable --now piano-capture.service
sudo systemctl enable --now piano-control.service
```

### Updating later

Nothing tracked needs editing per machine, so `git status` should be clean and
`git pull` should never conflict. If it does, something has been changed by
hand that ought to live in `audio.env` or `config.toml` instead.

After pulling anything that changes the Python or the units:

```bash
sudo cp systemd/*.service /etc/systemd/system/   # only if a unit changed
sudo systemctl daemon-reload
sudo systemctl restart piano-control piano-capture
```

`fluidsynth` only needs restarting if you changed its unit — leaving it alone
means audio keeps playing across an update of the front end.

---

## 12. Verify

```bash
systemctl status fluidsynth piano-capture piano-control
```

All three should be `active (running)`. Then watch the control app start up:

```bash
journalctl -u piano-control -f
```

You should see the soundfont count, a connection message, and a load
confirmation. The matrix does a brief green sweep when ready, then blanks.

**Play a key.** You should hear your soundfont through the USB sound card.

**Nudge the joystick.** The matrix wakes, lights the loaded font, and scrolls
its name.

**Hold the joystick in for 1.5s.** The matrix flashes amber then green, and a
file appears:

```bash
ls -l ~/recordings
```

Finally, `sudo reboot` and confirm it all comes back on its own. Nothing to log
into from here — power on and play.

---

# Configuration

Everything tunable lives in `config.toml`, and **every setting is documented
inline there** — that's deliberate, since it's the file you edit and comments
next to a value can't drift away from it. This is the map:

| Section | Controls | Detail |
|---|---|---|
| `[paths]` | Where soundfonts and saved state live. Uses `~`, so nothing depends on your username. | [step 9](#9-configure) |
| `[fluidsynth]` | Connection to the audio engine, load timeout, and `load_before_unload` ordering. | [step 9](#9-configure) |
| `[display]` | LED rotation, brightness, idle timeout, scroll speed. | below |
| `[colours]` | The palette. **Scrolling text must be a single channel** — see below. | below |
| `[shutdown]` | Hold-to-power-off gesture. | [Shutting down](#shutting-down) |
| `[pedal]` | Where the sustain pedal engages. | [The sustain pedal](#the-sustain-pedal) |
| `[capture]` | Rolling MIDI recording, buffer size, retention. | [MIDI recording](#midi-recording) |
| `[wifi]` | Optional hold-to-disable-WiFi toggle, off by default. | [Is the WiFi toggle worth it?](#is-the-wifi-toggle-worth-it) |

After changing anything: `sudo systemctl restart piano-control`. Only
`[fluidsynth]` settings baked into the *service file* need FluidSynth itself
restarted.

## A note on colours

Each Sense HAT pixel is one package containing three separate dies. A white or
mixed colour lights all three, so every pixel of a glyph renders as three
distinct coloured points side by side — which smears the edges of a 3x5
character and makes soundfont names genuinely hard to read. `low_light = true`
makes it worse, because the dies separate out more at low brightness.

**Keep `text` to a single channel** — `[0, n, 0]`, `[n, 0, 0]` or `[0, 0, n]`.
One channel lights one die: a single clean point. Green reads best, sitting at
the eye's peak sensitivity; red is a good alternative; blue is worst, because
the eye focuses short wavelengths poorly and the edges look soft however crisp
the LED is.

This only matters for `text`. The other colours are single pixels or
whole-matrix flashes, where die separation is invisible.

---

# MIDI recording

Everything the piano sends is kept in a ring buffer in memory. Nothing is
written to disk until you ask for it.

**To save:** hold the joystick in for about 1.5 seconds. The whole matrix
flashes amber to confirm the hold registered, then green when the file is
written (red if something went wrong). It works with the display asleep and
does not wake it, because you will be mid-playing and not looking at the HAT.

Files land in `~/recordings`, named by the moment you saved:

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
scp $USER@pi4Bsynth:recordings/\*.mid ~/Music/piano/
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

# Shutting down

Pulling the power on a running Pi risks corrupting the SD card, and reaching for
SSH defeats the point of an appliance. So: **hold the joystick down.**

The grid fills red one pixel at a time over five seconds. **Release at any point
and nothing happens** — the display clears and you carry on. Only a completely
full grid halts the machine, and the fill is its own confirmation prompt: you
can start one out of curiosity and simply let go.

The grid stays lit for the second or two systemd takes to stop the services,
then blanks. **A blank display is the signal that the services are down.** Give
it a couple of seconds more for the card to sync, then cut the power. The Pi's
own green ACT LED flashes ten times at the very end of shutdown if you want the
definitive answer.

> **Why the display clears at all.** The Sense HAT's LED matrix is driven by a
> microcontroller on the HAT that holds the last frame written to it, and a
> halted Pi keeps its 5V rail energised. So the matrix does not go dark by
> itself — whatever was on it when the service stopped stays lit indefinitely,
> drawing current, on a machine that looks switched off.
>
> `piano_control.py` handles SIGTERM for exactly this reason. systemd stops
> services with SIGTERM, and Python's default disposition terminates the
> process outright without running `finally` blocks. Turning it into
> `SystemExit` lets the cleanup path clear the matrix.
>
> Pulling the plug clears it too, obviously — no power, no LEDs. The case
> nothing can fix is a kernel panic or a hang: the board stays energised but
> nothing is running to clear the display, so the last frame simply stays there.

## It needs a sudoers rule

Halting requires root, so grant exactly that one command and nothing else:

```bash
sudo tee /etc/sudoers.d/piano-shutdown >/dev/null <<EOF
$USER ALL=(root) NOPASSWD: /sbin/poweroff
EOF
sudo chmod 440 /etc/sudoers.d/piano-shutdown
```

Without it the gesture still runs, but the grid flashes red three times at the
end and the journal explains why:

```
ERROR Shutdown refused. Is /etc/sudoers.d/piano-shutdown installed?
```

## Configuring it

```toml
[shutdown]
enabled = true
hold_direction = "down"
hold_seconds = 5.0
```

Set `enabled = false` to remove the gesture entirely. Lengthen `hold_seconds`
if five seconds feels too easy, or change `hold_direction` — but avoid whichever
direction `[wifi]` uses if you have that toggle enabled, since both are
hold-a-direction gestures.

**A known quirk:** pressing down also moves the cursor one row, because
direction presses act immediately. Suppressing that would mean deferring every
direction to release, which would make browsing feel laggy. The wifi toggle
behaves the same way.

---

# Levels

Set the volume at the **sound card**, not at FluidSynth. Getting this backwards
is audible.

FluidSynth applies `-g` to the *summed* output of every sounding voice. A gain
that sounds fine on a single note overflows full scale once ten notes are
ringing with the pedal down — and overflow is hard clipping, which sounds like
crackle. It costs no CPU, produces no underrun, and appears only on dense
passages, so it is easy to mistake for a performance problem.

Measured on a Pi 4 with a Yamaha Grand soundfont, against a deliberate worst
case of four ten-note chords with the sustain pedal held:

| `-g` | peak sample | headroom | clipped samples |
|---|---|---|---|
| 0.2 | 15360 | +6.6 dB | 0 |
| **0.3** | 24535 | **+2.5 dB** | **0** |
| 0.4 | 32714 | 0.0 dB | 1 |
| 0.5 | 32768 | — | 42 |
| 1.0 | 32768 | — | 6,131 |
| 2.0 | 32768 | — | 37,945 |

Earlier versions of this project shipped `-g 2.0`, which clips roughly forty
thousand samples in a ten-second passage. It now ships **`-g 0.3`**, which
leaves 2.5 dB spare on material harder than anyone actually plays.

**Make up the loudness at the card**, where attenuation costs nothing:

```bash
amixer -c S3 sset Speaker 100%      # substitute your own card name
sudo alsactl store                  # or it reverts at next boot
```

A USB card often ships heavily attenuated — this one defaulted to −20 dB,
which is precisely why someone reached for `-g 2.0` in the first place. Unity
at the card plus a conservative synth gain gives the same loudness with the
headroom intact.

Check what yours is doing:

```bash
amixer -c S3 sget Speaker
```

If you need more level still, take it from your amplifier. There is no
advantage to raising `-g` and a very audible cost.

---

# The sustain pedal

If sustain feels like it needs a deeper press through the Pi than it does
playing the piano directly, this is why.

MIDI treats CC64 as a **switch**: 64 or above is down, below is up. FluidSynth
follows that. But a piano with a half-damper pedal reports intermediate values
for partial positions — a Yamaha P-95 sends exactly three:

| Damper on the piano | CC64 | FluidSynth does |
|---|---|---|
| none | 0 | off |
| **partial** | **56** | **off** |
| full | 127 | on |

Two of three states agree. The middle one doesn't, because 56 falls eight
counts below the threshold — so the entire partial-damper zone reads as
pedal-up, and sustain arrives only once the pedal is pressed all the way down.

## Find your own value

```bash
aseqdump -p "USB MIDI Interface" | grep "controller 64"
```

Play with the pedal at various depths. If you only ever see `0` and `127`, your
pedal is a plain switch and none of this applies. If an intermediate value
appears, that's your half-damper position.

## Fix it

Set `sustain_threshold` under `[pedal]` in `config.toml` to that value:

```toml
[pedal]
sustain_threshold = 56
```

then `sudo systemctl restart piano-control`. The control app rewrites CC64 in
FluidSynth's MIDI router so the switch flips where your foot expects it.
Leaving it at 64 keeps standard behaviour and the router is not touched at all.

You should see it confirmed in the log:

```
INFO Sustain pedal engages at CC64 >= 56 (MIDI default is 64)
```

## What this cannot do

It moves where the switch flips. **It does not give you partial damping**, and
nothing in this stack can: SoundFont 2 has no parameter for a half-damped
string, so any value at or above the threshold is full sustain. You are
choosing where the on/off point sits, not gaining a third state.

If graduated damping is what you're missing — the note thinning rather than
switching — that needs a physically modelled engine such as Pianoteq, which
simulates damper-to-string contact directly.

Two things worth knowing:

- **Rules live in the running FluidSynth**, not in a file, so they are
  reapplied every time `piano-control` connects. `piano-control` declares
  `Requires=fluidsynth.service`, so if the synth restarts systemd restarts the
  control app too and the rules come back with it.
- **Your recordings are unaffected.** `piano-capture` taps the MIDI stream
  upstream of FluidSynth, so saved files hold the true `56`, not the rewritten
  `127`. The nuance your pedal produces is preserved on disk even though this
  playback path cannot render it — which matters if you ever replay those files
  through something that can.

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
sudo tee /etc/sudoers.d/piano-wifi >/dev/null <<EOF
$USER ALL=(root) NOPASSWD: /usr/sbin/rfkill block wifi, /usr/sbin/rfkill unblock wifi
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
`journalctl -u fluidsynth -n 50`.

`The "hw:CARD=..." audio device is used by another application`, usually paired
with `error 98 while trying to bind server socket`, means something else holds
the card and port 9800. By far the most likely culprit is the FluidSynth
service Debian ships — see step 3. Find out what has it:

```bash
pgrep -a fluidsynth
fuser -v /dev/snd/*
```

Very small buffer values can also make ALSA refuse the device; try
`period-size=256`.

**Debugging FluidSynth generally: run it by hand.** Errors are far easier to
read directly than through a unit that is restarting in a loop:

```bash
sudo systemctl stop fluidsynth
timeout 8 /usr/bin/fluidsynth -is -a alsa \
  -o audio.alsa.device=hw:CARD=S3,DEV=0 \
  -o audio.period-size=128 -o audio.periods=2 \
  -m alsa_seq -o midi.autoconnect=1 -o shell.port=9800
```

Substitute your own card string. If it stays up for the full 8 seconds with no
errors, the configuration is sound and the problem is in the unit file.

**Sound works but is very quiet.**
**Do not raise `-g`.** Turn up the card's mixer instead — `alsamixer -c S3`,
or `amixer -c S3 sset Speaker 100%`. See [Levels](#levels) for why that order
matters. Also check nothing is muted (F5 in alsamixer shows capture and
playback controls).

**Crackling only when you play a lot of notes at once.**
That is clipping, not a dropout. Voices sum before `-g` is applied, so a gain
that is fine for one note overflows on a ten-note chord. See
[Levels](#levels). Dropouts, by contrast, do not care how many notes are
sounding.

**Crackling or dropouts.**
In order: confirm `ulimit -r` is 90; confirm the performance governor is
active; raise `period-size` to 256; check `journalctl -u fluidsynth` for
underrun messages.

**Sense HAT shows nothing.**
`systemctl status piano-control`. If it's restarting in a loop,
`journalctl -u piano-control -n 50` will say why — usually the venv can't
import `sense_hat` (redo step 8) or it can't reach FluidSynth on port 9800.

**Sense HAT reports an I2C error, or `SenseHat()` raises on startup.**
Check the device nodes exist:

```bash
ls /dev/i2c-*
```

If they're missing but `ls /sys/bus/i2c/devices/` shows entries like `1-001c`
and `1-0046`, the bus is fine and the HAT is detected — you're only missing the
`i2c-dev` module. See step 2.

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
readable by `$USER`.

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
