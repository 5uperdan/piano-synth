#!/usr/bin/env python3
"""Sense HAT control surface for a headless FluidSynth soundfont player.

FluidSynth runs as its own systemd service with a TCP command shell open on
localhost.  This program is a separate process that connects to that shell and
drives it.  Keeping them apart means a crash or a restart here never
interrupts audio -- the piano keeps playing whatever is already loaded.

Interaction model
-----------------
The matrix is blank while idle.  Soundfonts are sorted alphabetically and laid
out left to right, one row at a time, so index i sits at row i // 8, column
i % 8.

  * First joystick nudge in any direction wakes the display and lights the
    currently loaded font, then scrolls its name.  It does not move the cursor.
  * Subsequent nudges move the cursor, each one lighting the new position and
    scrolling that font's name.
  * Any input cancels an animation in progress and starts the new one.
  * Pressing the joystick in loads the font under the cursor.
  * After `idle_timeout` with no input the display sleeps again, so the next
    nudge is a wake rather than a move.
"""

from __future__ import annotations

import json
import logging
import os
import re
import select
import socket
import subprocess
import sys
import threading
import time
import tomllib
from pathlib import Path

from font3x5 import blank_columns, render_text

LOG = logging.getLogger("piano")

MATRIX_SIZE = 8
MATRIX_PIXELS = MATRIX_SIZE * MATRIX_SIZE
BLACK = (0, 0, 0)

# Clockwise order, used to remap joystick directions when the HAT is mounted
# rotated relative to the player.
ROTATION_ORDER = ("up", "right", "down", "left")


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

class Config:
    def __init__(self, data: dict):
        paths = data.get("paths", {})
        self.soundfont_dir = Path(paths.get("soundfont_dir", "~/soundfonts")).expanduser()
        self.state_file = Path(
            paths.get("state_file", "~/.local/state/piano/state.json")
        ).expanduser()

        fluid = data.get("fluidsynth", {})
        self.host = fluid.get("host", "127.0.0.1")
        self.port = int(fluid.get("port", 9800))
        self.connect_retries = int(fluid.get("connect_retries", 60))
        self.connect_delay = float(fluid.get("connect_delay", 1.0))
        self.load_timeout = float(fluid.get("load_timeout", 180.0))
        self.load_before_unload = bool(fluid.get("load_before_unload", True))

        display = data.get("display", {})
        self.rotation = int(display.get("rotation", 0))
        self.joystick_rotation = int(display.get("joystick_rotation", 0))
        self.low_light = bool(display.get("low_light", True))
        self.idle_timeout = float(display.get("idle_timeout", 6.0))
        self.highlight_seconds = float(display.get("highlight_seconds", 0.5))
        self.scroll_step = float(display.get("scroll_step_seconds", 0.06))
        self.show_loaded_marker = bool(display.get("show_loaded_marker", True))
        self.show_ready_animation = bool(display.get("show_ready_animation", True))

        colours = data.get("colours", {})
        self.colour_cursor = tuple(colours.get("cursor", [0, 120, 255]))
        self.colour_loaded = tuple(colours.get("loaded", [0, 70, 0]))
        self.colour_text = tuple(colours.get("text", [110, 110, 110]))
        self.colour_success = tuple(colours.get("success", [0, 150, 0]))
        self.colour_error = tuple(colours.get("error", [150, 0, 0]))
        self.colour_busy = tuple(colours.get("busy", [140, 70, 0]))
        self.colour_wifi_on = tuple(colours.get("wifi_on", [0, 60, 160]))
        self.colour_wifi_off = tuple(colours.get("wifi_off", [90, 40, 0]))

        wifi = data.get("wifi", {})
        self.wifi_toggle_enabled = bool(wifi.get("toggle_enabled", False))
        self.wifi_hold_seconds = float(wifi.get("hold_seconds", 2.0))
        self.wifi_hold_direction = wifi.get("hold_direction", "up")


def load_config(path: Path) -> Config:
    if path.exists():
        with path.open("rb") as handle:
            return Config(tomllib.load(handle))
    LOG.warning("No config at %s, using defaults", path)
    return Config({})


# --------------------------------------------------------------------------
# FluidSynth client
# --------------------------------------------------------------------------

class FluidError(RuntimeError):
    pass


class FluidClient:
    """Thin wrapper around FluidSynth's line-oriented TCP command shell."""

    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port
        self.sock: socket.socket | None = None

    def connect(self, retries: int, delay: float) -> None:
        last_error: Exception | None = None
        for attempt in range(1, retries + 1):
            try:
                self.sock = socket.create_connection((self.host, self.port), timeout=5.0)
                self.sock.setblocking(False)
                # Swallow the greeting banner.
                self._drain(timeout=1.0)
                LOG.info("Connected to FluidSynth on %s:%d", self.host, self.port)
                return
            except OSError as exc:
                last_error = exc
                LOG.debug("Connect attempt %d/%d failed: %s", attempt, retries, exc)
                time.sleep(delay)
        raise FluidError(f"Could not reach FluidSynth after {retries} attempts: {last_error}")

    def _drain(self, timeout: float, quiet_for: float = 0.15) -> str:
        """Read until the socket has been silent for `quiet_for` seconds."""
        assert self.sock is not None
        chunks: list[bytes] = []
        deadline = time.monotonic() + timeout
        last_data: float | None = None
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            ready, _, _ = select.select([self.sock], [], [], min(0.1, remaining))
            if ready:
                try:
                    data = self.sock.recv(8192)
                except BlockingIOError:
                    continue
                if not data:
                    break
                chunks.append(data)
                last_data = time.monotonic()
            elif last_data is not None and time.monotonic() - last_data > quiet_for:
                break
        return b"".join(chunks).decode("utf-8", errors="replace")

    def command(self, text: str, timeout: float = 3.0) -> str:
        if self.sock is None:
            raise FluidError("Not connected")
        self.sock.sendall((text + "\n").encode("utf-8"))
        return self._drain(timeout=timeout)

    # -- soundfont handling ------------------------------------------------

    def loaded_font_ids(self) -> list[int]:
        response = self.command("fonts")
        return [int(match) for match in re.findall(r"^\s*(\d+)\s+\S", response, re.MULTILINE)]

    def _first_preset(self, font_id: int) -> tuple[int, int]:
        """Return (bank, program) of the font's first preset, defaulting to 0,0."""
        response = self.command(f"inst {font_id}")
        match = re.search(r"(\d+)\s*-\s*(\d+)", response)
        if match:
            return int(match.group(1)), int(match.group(2))
        return 0, 0

    def switch_font(self, path: Path, load_first: bool, load_timeout: float) -> int:
        """Make `path` the active soundfont. Returns its FluidSynth font ID."""
        previous = self.loaded_font_ids()

        if not load_first:
            for font_id in previous:
                self.command(f"unload {font_id}", timeout=30.0)
            previous = []

        response = self.command(f'load "{path}"', timeout=load_timeout)
        match = re.search(r"ID\s+(\d+)", response)
        if match:
            new_id = int(match.group(1))
        else:
            # Older builds phrase it differently; fall back to diffing the list.
            candidates = [i for i in self.loaded_font_ids() if i not in previous]
            if not candidates:
                raise FluidError(f"Load failed for {path.name}: {response.strip()[:200]}")
            new_id = max(candidates)

        # All notes off before we retarget channels, so nothing hangs.
        self.command("reset")

        bank, program = self._first_preset(new_id)
        for channel in range(16):
            self.command(f"select {channel} {new_id} {bank} {program}", timeout=1.0)

        for font_id in previous:
            self.command(f"unload {font_id}", timeout=30.0)

        LOG.info("Loaded %s as font %d (bank %d, program %d)", path.name, new_id, bank, program)
        return new_id


# --------------------------------------------------------------------------
# Display
# --------------------------------------------------------------------------

def index_to_xy(index: int) -> tuple[int, int]:
    return index % MATRIX_SIZE, index // MATRIX_SIZE


class Matrix:
    """Runs one cancellable animation at a time on the LED matrix."""

    def __init__(self, sense, config: Config):
        self.sense = sense
        self.config = config
        self._thread: threading.Thread | None = None
        self._cancel = threading.Event()
        self._lock = threading.Lock()

    # -- immediate, non-threaded drawing -----------------------------------

    def clear(self) -> None:
        self.sense.clear()

    def show_marks(self, marks: dict[int, tuple[int, int, int]]) -> None:
        frame = [BLACK] * MATRIX_PIXELS
        for index, colour in marks.items():
            if 0 <= index < MATRIX_PIXELS:
                frame[index] = colour
        self.sense.set_pixels(frame)

    def flash(self, index: int, colour, times: int, on: float, off: float) -> None:
        for _ in range(times):
            self.show_marks({index: colour})
            time.sleep(on)
            self.clear()
            time.sleep(off)

    # -- cancellable animations --------------------------------------------

    def cancel(self) -> None:
        with self._lock:
            self._cancel.set()
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)

    def start(self, animation) -> None:
        """Cancel anything running and start `animation(token)` in a thread."""
        self.cancel()
        with self._lock:
            self._cancel = threading.Event()
            token = self._cancel
            self._thread = threading.Thread(
                target=self._run, args=(animation, token), daemon=True
            )
            thread = self._thread
        thread.start()

    def _run(self, animation, token: threading.Event) -> None:
        try:
            animation(token)
        except Exception:  # noqa: BLE001 - never let the display kill the app
            LOG.exception("Animation failed")
        finally:
            if not token.is_set():
                self.clear()

    def sleep(self, token: threading.Event, seconds: float) -> bool:
        """Sleep unless cancelled. Returns False if cancelled."""
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if token.is_set():
                return False
            time.sleep(0.01)
        return not token.is_set()

    def scroll(self, token: threading.Event, text: str, colour) -> bool:
        columns = blank_columns(MATRIX_SIZE) + render_text(text) + blank_columns(MATRIX_SIZE)
        for start in range(len(columns) - MATRIX_SIZE + 1):
            if token.is_set():
                return False
            frame: list[tuple[int, int, int]] = []
            for row in range(MATRIX_SIZE):
                for col in range(MATRIX_SIZE):
                    frame.append(colour if columns[start + col][row] else BLACK)
            self.sense.set_pixels(frame)
            if not self.sleep(token, self.config.scroll_step):
                return False
        return True


# --------------------------------------------------------------------------
# WiFi
# --------------------------------------------------------------------------

def wifi_blocked() -> bool | None:
    try:
        output = subprocess.run(
            ["rfkill", "list", "wifi"], capture_output=True, text=True, timeout=5
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r"Soft blocked:\s*(yes|no)", output)
    if not match:
        return None
    return match.group(1) == "yes"


def set_wifi(blocked: bool) -> bool:
    action = "block" if blocked else "unblock"
    try:
        result = subprocess.run(
            ["sudo", "-n", "rfkill", action, "wifi"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        LOG.error("rfkill %s failed: %s", action, exc)
        return False
    if result.returncode != 0:
        LOG.error("rfkill %s failed: %s", action, result.stderr.strip())
        return False
    LOG.info("WiFi %s", "disabled" if blocked else "enabled")
    return True


# --------------------------------------------------------------------------
# Application
# --------------------------------------------------------------------------

class App:
    def __init__(self, config: Config, sense, fluid: FluidClient):
        self.config = config
        self.sense = sense
        self.fluid = fluid
        self.matrix = Matrix(sense, config)

        self.fonts: list[Path] = []
        self.cursor = 0
        self.loaded_index: int | None = None
        self.awake = False
        self.last_input = 0.0
        self._hold_started: float | None = None

    # -- soundfont inventory ------------------------------------------------

    def scan(self) -> None:
        directory = self.config.soundfont_dir
        found = sorted(
            (p for p in directory.glob("*") if p.suffix.lower() in {".sf2", ".sf3"}),
            key=lambda p: p.name.lower(),
        )
        self.fonts = found[:MATRIX_PIXELS]
        if len(found) > MATRIX_PIXELS:
            LOG.warning(
                "%d soundfonts found but only %d fit the grid; ignoring the rest",
                len(found),
                MATRIX_PIXELS,
            )
        LOG.info("Found %d soundfont(s) in %s", len(self.fonts), directory)

    def wait_for_fonts(self) -> None:
        while True:
            self.scan()
            if self.fonts:
                return
            LOG.error("No soundfonts in %s; rescanning in 15s", self.config.soundfont_dir)
            self.matrix.show_marks({0: self.config.colour_error})
            time.sleep(15)

    # -- persisted state ----------------------------------------------------

    def read_state(self) -> str | None:
        try:
            with self.config.state_file.open() as handle:
                return json.load(handle).get("last_font")
        except (OSError, ValueError):
            return None

    def write_state(self) -> None:
        if self.loaded_index is None:
            return
        try:
            self.config.state_file.parent.mkdir(parents=True, exist_ok=True)
            with self.config.state_file.open("w") as handle:
                json.dump({"last_font": self.fonts[self.loaded_index].name}, handle)
        except OSError as exc:
            LOG.warning("Could not save state: %s", exc)

    # -- loading ------------------------------------------------------------

    def load_index(self, index: int, announce: bool) -> bool:
        path = self.fonts[index]
        self.matrix.cancel()

        if announce:
            self.matrix.flash(index, self.config.colour_busy, times=3, on=0.08, off=0.08)
        self.matrix.show_marks({index: self.config.colour_busy})

        try:
            self.fluid.switch_font(
                path,
                load_first=self.config.load_before_unload,
                load_timeout=self.config.load_timeout,
            )
        except (FluidError, OSError) as exc:
            LOG.error("Failed to load %s: %s", path.name, exc)
            self.matrix.flash(index, self.config.colour_error, times=3, on=0.15, off=0.1)
            self.matrix.clear()
            return False

        self.loaded_index = index
        self.write_state()
        self.matrix.flash(index, self.config.colour_success, times=2, on=0.1, off=0.08)
        self.matrix.clear()
        return True

    # -- animations ---------------------------------------------------------

    def announce_cursor(self) -> None:
        index = self.cursor
        name = self.fonts[index].stem

        def animation(token: threading.Event) -> None:
            marks = {index: self.config.colour_cursor}
            if (
                self.config.show_loaded_marker
                and self.loaded_index is not None
                and self.loaded_index != index
            ):
                marks[self.loaded_index] = self.config.colour_loaded
            self.matrix.show_marks(marks)
            if not self.matrix.sleep(token, self.config.highlight_seconds):
                return
            self.matrix.scroll(token, name, self.config.colour_text)

        self.matrix.start(animation)

    def ready_animation(self) -> None:
        if not self.config.show_ready_animation:
            return
        for row in range(MATRIX_SIZE):
            self.matrix.show_marks(
                {row * MATRIX_SIZE + col: self.config.colour_loaded for col in range(MATRIX_SIZE)}
            )
            time.sleep(0.04)
        self.matrix.clear()

    # -- input --------------------------------------------------------------

    def rotate(self, direction: str) -> str:
        if direction not in ROTATION_ORDER or self.config.joystick_rotation % 360 == 0:
            return direction
        steps = (self.config.joystick_rotation // 90) % 4
        return ROTATION_ORDER[(ROTATION_ORDER.index(direction) + steps) % 4]

    def move_cursor(self, direction: str) -> None:
        count = len(self.fonts)
        x, y = index_to_xy(self.cursor)
        if direction == "left":
            candidate = self.cursor - 1
        elif direction == "right":
            candidate = self.cursor + 1
        elif direction == "up":
            candidate = self.cursor - MATRIX_SIZE
        elif direction == "down":
            candidate = self.cursor + MATRIX_SIZE
        else:
            return
        if 0 <= candidate < count:
            self.cursor = candidate

    def handle_press(self, direction: str) -> None:
        self.last_input = time.monotonic()

        if direction == "middle":
            if self.awake and self.cursor != self.loaded_index:
                self.load_index(self.cursor, announce=True)
            elif self.awake:
                # Already loaded; acknowledge without reloading.
                self.matrix.cancel()
                self.matrix.flash(self.cursor, self.config.colour_loaded, 2, 0.08, 0.08)
                self.matrix.clear()
            else:
                self.awake = True
                self.announce_cursor()
            return

        if not self.awake:
            self.awake = True
            if self.loaded_index is not None:
                self.cursor = self.loaded_index
        else:
            self.move_cursor(self.rotate(direction))
        self.announce_cursor()

    def handle_hold(self, direction: str, now: float) -> None:
        if not self.config.wifi_toggle_enabled:
            return
        if self.rotate(direction) != self.config.wifi_hold_direction:
            self._hold_started = None
            return
        if self._hold_started is None:
            self._hold_started = now
            return
        if now - self._hold_started < self.config.wifi_hold_seconds:
            return
        self._hold_started = None

        blocked = wifi_blocked()
        target = not bool(blocked)
        if set_wifi(target):
            colour = self.config.colour_wifi_off if target else self.config.colour_wifi_on
            self.matrix.cancel()
            self.matrix.show_marks({i: colour for i in range(MATRIX_PIXELS)})
            time.sleep(0.8)
            self.matrix.clear()
            self.last_input = time.monotonic()

    # -- main loop ----------------------------------------------------------

    def run(self) -> None:
        self.wait_for_fonts()

        remembered = self.read_state()
        start_index = 0
        if remembered:
            for i, path in enumerate(self.fonts):
                if path.name == remembered:
                    start_index = i
                    break

        self.cursor = start_index
        self.load_index(start_index, announce=False)
        self.ready_animation()
        self.matrix.clear()

        # Ignore anything the joystick collected during startup.
        self.sense.stick.get_events()

        while True:
            now = time.monotonic()
            for event in self.sense.stick.get_events():
                if event.action == "pressed":
                    self._hold_started = None
                    self.handle_press(event.direction)
                    # Loading blocks; discard input that queued up meanwhile.
                    if event.direction == "middle":
                        self.sense.stick.get_events()
                elif event.action == "held":
                    self.handle_hold(event.direction, time.monotonic())
                elif event.action == "released":
                    self._hold_started = None

            if self.awake and now - self.last_input > self.config.idle_timeout:
                self.awake = False
                self.matrix.cancel()
                self.matrix.clear()

            time.sleep(0.02)


# --------------------------------------------------------------------------

def main() -> int:
    logging.basicConfig(
        level=os.environ.get("PIANO_LOG_LEVEL", "INFO"),
        format="%(levelname)s %(message)s",
        stream=sys.stdout,
    )

    config_path = Path(
        os.environ.get("PIANO_CONFIG", Path(__file__).resolve().parent / "config.toml")
    )
    config = load_config(config_path)

    # Always restore WiFi at startup so a reboot can never lock you out.
    if config.wifi_toggle_enabled and wifi_blocked():
        set_wifi(False)

    from sense_hat import SenseHat  # imported late so config errors surface first

    sense = SenseHat()
    sense.set_rotation(config.rotation)
    sense.low_light = config.low_light
    sense.clear()

    fluid = FluidClient(config.host, config.port)
    try:
        fluid.connect(config.connect_retries, config.connect_delay)
    except FluidError as exc:
        LOG.error("%s", exc)
        return 1

    app = App(config, sense, fluid)
    try:
        app.run()
    except KeyboardInterrupt:
        pass
    finally:
        sense.clear()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
