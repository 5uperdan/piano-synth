#!/usr/bin/env python3
"""The shared MIDI tap.

Two services listen to the piano: `midi_capture` records what you play, and
`cpu_boost` watches for activity so it can raise the CPU governor. Both
subscribe to the same ALSA sequencer port in parallel -- the kernel delivers
each event to every subscriber independently, so a second listener costs
nothing and cannot affect the first.

This module is the part they share. It knows how to find the piano's port,
keep it open, and hand musical events to a callback. It knows nothing about
what either service then does with them.
"""

from __future__ import annotations

import logging
import threading
import time

LOG = logging.getLogger("midi")

# How often to check the MIDI adapter is still plugged in.
PORT_POLL_SECONDS = 5.0

# Channel voice messages only. 0xF0 and above is system realtime: clock,
# active sensing, start/stop. A P-95 emits a steady stream of those -- measured
# at 97% of everything it sends -- and they carry no musical information, so
# they are dropped before reaching either consumer.
STATUS_MIN = 0x80
STATUS_MAX = 0xEF


def is_musical(data: bytes) -> bool:
    return bool(data) and STATUS_MIN <= data[0] <= STATUS_MAX


class MidiSource:
    """Keeps an rtmidi input port open, reopening it if the adapter vanishes.

    Worth having on a box you never log into: unplug the MIDI cable without
    this and recording stops silently until someone restarts the service.
    Detection is by polling the port list -- rtmidi's own `is_port_open` only
    reports whether we called `open_port`, not whether the device is still
    there.
    """

    def __init__(self, on_event, port_match: str = "MIDI"):
        """`on_event(timestamp, data)` is called for each musical message, on
        rtmidi's own thread. Keep it short -- anything slow belongs on a
        different thread."""
        self.on_event = on_event
        self.port_match = port_match
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
        wanted = self.port_match.lower()
        for index, name in candidates:
            if wanted and wanted in name.lower():
                return index
        return candidates[0][0] if candidates else None

    def _on_message(self, message, _data=None) -> None:
        payload, _delta = message
        data = bytes(payload)
        if is_musical(data):
            self.on_event(time.monotonic(), data)

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
            LOG.warning("No MIDI input port matching %r", self.port_match)
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
