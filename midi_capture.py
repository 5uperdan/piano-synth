#!/usr/bin/env python3
"""Rolling MIDI capture for the headless piano module.

Everything the piano sends is kept in a ring buffer in memory.  Nothing is
written to disk until you ask for it, by holding the Sense HAT joystick in.
At that point the whole buffer is dumped to a timestamped Standard MIDI File.

This runs as its own service, separate from both FluidSynth and the Sense HAT
front end, for two reasons:

  * It subscribes to the same ALSA sequencer port FluidSynth is already
    listening to, in parallel.  The kernel delivers each event to both
    independently, so nothing here can delay audio.  If this process stalls,
    its own FIFO overflows and it loses its own events -- FluidSynth is
    untouched.
  * piano_control.py blocks for seconds at a time while a soundfont loads.
    Capturing from that loop would punch a hole in the recording every time
    you changed sound.

The soundfont in use is deliberately not recorded anywhere.  Capture happens
upstream of FluidSynth, so the bytes are identical whichever font is loaded.
"""

from __future__ import annotations

import argparse
import datetime
import logging
import os
import socketserver
import threading
import time
import tomllib
from collections import deque
from pathlib import Path

LOG = logging.getLogger("capture")

# 480 ticks per beat at 120bpm puts a tick just under a millisecond, which is
# finer than anything a human plays and finer than MIDI itself can deliver.
TICKS_PER_BEAT = 480
TEMPO_US_PER_BEAT = 500_000
TICKS_PER_SECOND = TICKS_PER_BEAT * 1_000_000 / TEMPO_US_PER_BEAT

SECONDS_PER_DAY = 86_400

# How often to check the MIDI adapter is still plugged in.
PORT_POLL_SECONDS = 5.0

# Channel voice messages only.  0xF0 and above is system realtime: clock,
# active sensing, start/stop.  The P-95 emits a steady stream of those and
# they carry no musical information, so they are dropped before they ever
# reach the buffer rather than bloating it.
STATUS_MIN = 0x80
STATUS_MAX = 0xEF


def is_musical(data: bytes) -> bool:
    return bool(data) and STATUS_MIN <= data[0] <= STATUS_MAX


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

class CaptureConfig:
    def __init__(self, data: dict):
        capture = data.get("capture", {})
        self.enabled = bool(capture.get("enabled", True))
        self.socket_path = Path(capture.get("socket_path", "/run/piano/capture.sock"))
        self.recordings_dir = Path(
            capture.get("recordings_dir", "~/recordings")
        ).expanduser()
        self.window_minutes = float(capture.get("window_minutes", 60))
        self.max_events = int(capture.get("max_events", 250_000))
        self.retention_days = int(capture.get("retention_days", 30))
        self.port_match = str(capture.get("port_match", "MIDI"))

    @property
    def window_seconds(self) -> float:
        return self.window_minutes * 60.0


def load_config(path: Path) -> CaptureConfig:
    if path.exists():
        with path.open("rb") as handle:
            return CaptureConfig(tomllib.load(handle))
    LOG.warning("No config at %s, using defaults", path)
    return CaptureConfig({})


# --------------------------------------------------------------------------
# Ring buffer
# --------------------------------------------------------------------------

class RingBuffer:
    """Bounded two ways: by wall time, and by a hard event ceiling.

    The time window is the working policy -- it decides how much playing a
    saved file contains.  `max_events` exists only so that running out of
    memory is structurally impossible no matter how densely someone plays; in
    normal use it is never reached.
    """

    def __init__(self, window_seconds: float, max_events: int):
        self.window_seconds = window_seconds
        self._events: deque[tuple[float, bytes]] = deque(maxlen=max_events)
        self._lock = threading.Lock()

    def append(self, timestamp: float, data: bytes) -> None:
        with self._lock:
            self._events.append((timestamp, data))
            cutoff = timestamp - self.window_seconds
            while self._events and self._events[0][0] < cutoff:
                self._events.popleft()

    def snapshot(self) -> list[tuple[float, bytes]]:
        """A copy of the buffer. Cheap, so the capture thread barely blocks."""
        with self._lock:
            return list(self._events)

    def __len__(self) -> int:
        with self._lock:
            return len(self._events)


# --------------------------------------------------------------------------
# Standard MIDI File output
# --------------------------------------------------------------------------

def events_to_midifile(events):
    """Turn captured events into a format 0 SMF.

    Real elapsed seconds are encoded as ticks, so the file plays back at the
    speed it was performed regardless of what tempo a DAW displays.
    """
    import mido  # imported here so the module loads without it installed

    midi_file = mido.MidiFile(type=0, ticks_per_beat=TICKS_PER_BEAT)
    track = mido.MidiTrack()
    midi_file.tracks.append(track)
    track.append(mido.MetaMessage("set_tempo", tempo=TEMPO_US_PER_BEAT, time=0))

    if not events:
        return midi_file

    start = events[0][0]
    previous_tick = 0
    for timestamp, data in events:
        try:
            message = mido.Message.from_bytes(bytes(data))
        except (ValueError, IndexError):
            LOG.debug("Skipping undecodable event %r", data)
            continue
        tick = int(round((timestamp - start) * TICKS_PER_SECOND))
        delta = tick - previous_tick
        if delta < 0:
            delta = 0
        message.time = delta
        previous_tick += delta
        track.append(message)

    return midi_file


def timestamped_name(now: datetime.datetime | None = None) -> str:
    """Seconds resolution, because saves are non-destructive and you may well
    trigger two in the same minute."""
    moment = now or datetime.datetime.now()
    return moment.strftime("%Y-%m-%d_%H-%M-%S") + ".mid"


# --------------------------------------------------------------------------
# Retention
# --------------------------------------------------------------------------

def prune_recordings(directory: Path, retention_days: int, now: float | None = None):
    """Delete .mid files older than the retention window.

    Runs after each successful save so the directory can never quietly grow
    until it fills the card.  Deliberately narrow: only `*.mid`, only regular
    files, only the top level of the directory, and only when a positive
    retention is configured -- `retention_days = 0` disables pruning entirely.
    """
    if retention_days <= 0:
        return []

    cutoff = (now if now is not None else time.time()) - retention_days * SECONDS_PER_DAY
    removed: list[Path] = []
    for path in sorted(directory.glob("*.mid")):
        try:
            if not path.is_file():
                continue
            if path.stat().st_mtime >= cutoff:
                continue
            path.unlink()
            removed.append(path)
        except OSError as exc:
            LOG.warning("Could not remove %s: %s", path, exc)
    if removed:
        LOG.info("Pruned %d recording(s) older than %d days", len(removed), retention_days)
    return removed


# --------------------------------------------------------------------------
# Saving
# --------------------------------------------------------------------------

class Recorder:
    def __init__(self, buffer: RingBuffer, config: CaptureConfig):
        self.buffer = buffer
        self.config = config

    def save(self) -> Path:
        events = self.buffer.snapshot()
        if not events:
            raise ValueError("buffer is empty")

        self.config.recordings_dir.mkdir(parents=True, exist_ok=True)
        target = self.config.recordings_dir / timestamped_name()

        events_to_midifile(events).save(str(target))

        span = events[-1][0] - events[0][0]
        LOG.info("Saved %s (%d events, %.1fs)", target.name, len(events), span)

        prune_recordings(self.config.recordings_dir, self.config.retention_days)
        return target


# --------------------------------------------------------------------------
# Control socket
# --------------------------------------------------------------------------

class RequestHandler(socketserver.StreamRequestHandler):
    timeout = 10

    def handle(self) -> None:
        line = self.rfile.readline(256).decode("utf-8", errors="replace").strip()
        recorder: Recorder = self.server.recorder  # type: ignore[attr-defined]

        if line == "save":
            try:
                target = recorder.save()
            except Exception as exc:  # noqa: BLE001 - report, never die
                LOG.error("Save failed: %s", exc)
                self.wfile.write(f"ERR {exc}\n".encode())
            else:
                self.wfile.write(f"OK {target.name}\n".encode())
        elif line == "status":
            self.wfile.write(f"OK {len(recorder.buffer)} events\n".encode())
        else:
            self.wfile.write(b"ERR unknown command\n")


class ControlServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, path: Path, recorder: Recorder):
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            path.unlink()
        super().__init__(str(path), RequestHandler)
        os.chmod(path, 0o660)
        self.recorder = recorder


# --------------------------------------------------------------------------
# MIDI input
# --------------------------------------------------------------------------

class MidiSource:
    """Keeps an rtmidi input port open, reopening it if the adapter vanishes.

    Worth having on a box you never log into: unplug the MIDI cable without
    this and recording stops silently until someone restarts the service.
    Detection is by polling the port list -- rtmidi's own `is_port_open` only
    reports whether we called `open_port`, not whether the device is still
    there.
    """

    def __init__(self, buffer: RingBuffer, config: CaptureConfig):
        self.buffer = buffer
        self.config = config
        self._midi_in = None
        self._port_name: str | None = None
        self._stop = threading.Event()

    def _choose_port(self, names: list[str]) -> int | None:
        # "Midi Through" is ALSA's virtual loopback -- it matches most
        # substrings people would configure and never carries piano data.
        candidates = [
            (index, name)
            for index, name in enumerate(names)
            if "through" not in name.lower()
        ]
        wanted = self.config.port_match.lower()
        for index, name in candidates:
            if wanted and wanted in name.lower():
                return index
        return candidates[0][0] if candidates else None

    def _on_message(self, message, _data=None) -> None:
        payload, _delta = message
        data = bytes(payload)
        if is_musical(data):
            self.buffer.append(time.monotonic(), data)

    def _list_ports(self) -> list[str]:
        import rtmidi

        probe = rtmidi.MidiIn()
        try:
            return probe.get_ports()
        finally:
            probe.delete()

    def _open(self, names: list[str]) -> None:
        import rtmidi

        index = self._choose_port(names)
        if index is None:
            LOG.warning("No MIDI input port matching %r", self.config.port_match)
            return

        midi_in = rtmidi.MidiIn()
        midi_in.open_port(index)
        # rtmidi drops these by default and is_musical() would drop them
        # anyway, but not queueing them at all is cheapest.
        midi_in.ignore_types(sysex=True, timing=True, active_sense=True)
        midi_in.set_callback(self._on_message)
        self._midi_in = midi_in
        self._port_name = names[index]
        LOG.info("Listening on MIDI port %r", self._port_name)

    def _close(self) -> None:
        if self._midi_in is not None:
            try:
                self._midi_in.close_port()
                self._midi_in.delete()
            except Exception:  # noqa: BLE001
                pass
        self._midi_in = None
        self._port_name = None

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                names = self._list_ports()
                if self._port_name is not None and self._port_name not in names:
                    LOG.warning("MIDI port %r disappeared", self._port_name)
                    self._close()
                if self._midi_in is None:
                    self._open(names)
            except Exception as exc:  # noqa: BLE001 - never let this thread die
                LOG.error("MIDI input error: %s", exc)
                self._close()
            self._stop.wait(PORT_POLL_SECONDS)

    def stop(self) -> None:
        self._stop.set()
        self._close()


# --------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=os.environ.get("PIANO_CONFIG"))
    args = parser.parse_args()

    logging.basicConfig(
        level=os.environ.get("PIANO_LOG_LEVEL", "INFO"),
        format="%(levelname)s %(message)s",
    )

    config_path = Path(args.config or Path(__file__).resolve().parent / "config.toml")
    config = load_config(config_path)

    if not config.enabled:
        LOG.info("Capture disabled in %s; exiting", config_path)
        return 0

    buffer = RingBuffer(config.window_seconds, config.max_events)
    recorder = Recorder(buffer, config)

    source = MidiSource(buffer, config)
    threading.Thread(target=source.run, name="midi-in", daemon=True).start()

    server = ControlServer(config.socket_path, recorder)
    LOG.info(
        "Capturing to a %.0f minute buffer; save socket at %s",
        config.window_minutes,
        config.socket_path,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        source.stop()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
