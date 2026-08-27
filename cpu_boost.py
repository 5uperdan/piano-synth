#!/usr/bin/env python3
"""Hold the CPU at full clock for as long as you are playing.

FluidSynth's load is bursty and spread across three render threads, so
per-core utilisation reads around 10% even during dense chords. `ondemand`
never crosses its ramp-up threshold and leaves the cores at 600-700MHz, which
is a third of the compute per audio period -- a chord that renders in 1ms at
full clock needs 3ms against a 2.67ms deadline, and misses it audibly.

Pinning the governor to `performance` permanently fixes that, at the cost of
holding 1800MHz and 0.926V around the clock on a machine that is idle almost
all the time.

What no stock governor offers is hysteresis. `ondemand` and `schedutil` react
to instantaneous load, so a quiet bar in the middle of a piece looks exactly
like having gone to bed. This service adds the missing behaviour: any note
raises the governor, and it stays raised until you have genuinely stopped.

The first note after a silence is still rendered at the low clock -- the boost
is reacting to that note, and nothing here can anticipate it. That trade is
deliberate: crackle on a first note after several minutes is tolerable in a way
that crackle mid-phrase is not.

It listens on the same ALSA sequencer port as the recorder, in parallel. The
kernel delivers each event to both independently, so this costs the audio path
nothing.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import subprocess
import sys
import threading
import time
import tomllib
from pathlib import Path

from midi_source import PORT_POLL_SECONDS, MidiSource  # noqa: F401

LOG = logging.getLogger("boost")

# The unit that actually touches scaling_governor. Starting it sets
# `performance`; stopping it restores `ondemand`. Keeping all governor
# knowledge there means this service needs permission for two exact systemctl
# commands and nothing more -- it cannot set the governor to anything the unit
# does not already do.
GOVERNOR_UNIT = "cpu-performance.service"


class BoostConfig:
    def __init__(self, data: dict):
        perf = data.get("performance", {})
        self.enabled = bool(perf.get("boost_enabled", True))
        self.idle_release = float(perf.get("idle_release_seconds", 180))
        self.port_match = str(data.get("midi", {}).get("port_match", "MIDI"))


def load_config(path: Path) -> BoostConfig:
    if path.exists():
        with path.open("rb") as handle:
            return BoostConfig(tomllib.load(handle))
    LOG.warning("No config at %s, using defaults", path)
    return BoostConfig({})


class Governor:
    """Raises and lowers the clock by starting and stopping a systemd unit."""

    def __init__(self, unit: str = GOVERNOR_UNIT):
        self.unit = unit

    def _systemctl(self, action: str) -> bool:
        try:
            result = subprocess.run(
                ["sudo", "-n", "/usr/bin/systemctl", action, self.unit],
                capture_output=True, text=True, timeout=15,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            LOG.error("systemctl %s %s failed: %s", action, self.unit, exc)
            return False
        if result.returncode != 0:
            LOG.error("systemctl %s %s failed: %s", action, self.unit,
                      result.stderr.strip() or "no sudoers rule?")
            return False
        return True

    def boost(self) -> bool:
        return self._systemctl("start")

    def release(self) -> bool:
        return self._systemctl("stop")


class BoostController:
    """Tracks playing activity and actuates the governor.

    The MIDI callback only stamps a time and wakes the loop -- it must never
    block, because it runs on rtmidi's delivery thread and `systemctl` takes
    of the order of a hundred milliseconds. All the slow work happens here.
    """

    def __init__(self, governor: Governor, idle_release: float):
        self.governor = governor
        self.idle_release = idle_release
        self.last_activity: float | None = None
        self.boosted = False
        self._wake = threading.Event()
        self._stop = threading.Event()

    def on_event(self, timestamp: float, _data: bytes) -> None:
        self.last_activity = timestamp
        if not self.boosted:
            self._wake.set()

    def playing(self, now: float) -> bool:
        return (
            self.last_activity is not None
            and now - self.last_activity < self.idle_release
        )

    def step(self, now: float) -> None:
        """Bring the governor into line with whether you are playing."""
        active = self.playing(now)
        if active == self.boosted:
            return

        if active:
            applied = self.governor.boost()
            message = "Playing detected: CPU governor raised"
        else:
            applied = self.governor.release()
            message = f"Idle for {self.idle_release:.0f}s: CPU governor released"

        # Only believe the state changed if systemd agreed. A failure here is
        # usually a missing sudoers rule, and leaving `boosted` alone means the
        # next tick tries again rather than silently assuming success.
        if applied:
            self.boosted = active
            LOG.info("%s", message)

    def run(self) -> None:
        while not self._stop.is_set():
            self.step(time.monotonic())
            # Woken immediately by a note, otherwise ticks slowly enough to
            # notice the idle timeout without spinning.
            self._wake.wait(timeout=min(5.0, max(1.0, self.idle_release / 10)))
            self._wake.clear()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()


def main() -> int:
    parser = argparse.ArgumentParser(description="CPU governor boost on MIDI activity")
    parser.add_argument("--config", default=os.environ.get("PIANO_CONFIG"))
    args = parser.parse_args()

    logging.basicConfig(
        level=os.environ.get("PIANO_LOG_LEVEL", "INFO"),
        format="%(levelname)s %(message)s",
    )

    config_path = Path(args.config or Path(__file__).resolve().parent / "config.toml")
    config = load_config(config_path)

    if not config.enabled:
        LOG.info("CPU boost disabled in %s; exiting", config_path)
        return 0

    governor = Governor()
    controller = BoostController(governor, config.idle_release)

    source = MidiSource(controller.on_event, config.port_match)
    threading.Thread(target=source.run, name="midi-in", daemon=True).start()

    signal.signal(signal.SIGTERM, lambda _s, _f: sys.exit(0))
    LOG.info(
        "Watching for activity; releasing the governor after %.0fs idle",
        config.idle_release,
    )
    try:
        controller.run()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        source.stop()
        # Leave the machine in the low-power state on a clean exit. If this
        # fails the governor simply stays raised, which is the safe direction.
        if controller.boosted:
            governor.release()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
