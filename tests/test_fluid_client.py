"""Tests for the FluidSynth shell client.

The thing that makes this protocol awkward is that FluidSynth prints *nothing*
for commands that succeed silently -- `select`, `reset`, `unload` -- and offers
no prompt or greeting either. So there is no way to distinguish "no output yet"
from "no output ever" except by waiting, and a naive client waits out its whole
timeout on every one of them.

switch_font issues seventeen such commands, which cost about nineteen seconds
of dead waiting per soundfont change before this was fixed. The fake below
reproduces the silence exactly, so the regression can't come back unnoticed.
"""

import socket
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from piano_control import FluidClient, FluidError

SILENT = {"select", "reset", "unload", "gain", "noteon", "noteoff"}


class FakeFluidSynth:
    """A stand-in for FluidSynth's TCP shell, silence and all."""

    def __init__(self):
        self.listener = socket.socket()
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(1)
        self.port = self.listener.getsockname()[1]
        self.received: list[str] = []
        self.fonts: dict[int, str] = {}
        self._next_id = 1
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _respond(self, line: str) -> str:
        verb = line.split()[0] if line.split() else ""
        if verb in SILENT:
            if verb == "unload":
                self.fonts.pop(int(line.split()[1]), None)
            return ""                       # the whole point: no output
        if verb == "fonts":
            body = "".join(f"{i}  {p}\n" for i, p in sorted(self.fonts.items()))
            return "ID  Name\n" + body
        if verb == "inst":
            return "000-000 Some Preset\n000-001 Another\n"
        if verb == "load":
            self._next_id += 1
            self.fonts[self._next_id] = line.split('"')[1]
            return f"fluidsynth: loaded SoundFont has ID {self._next_id}\n"
        return f"unknown command: {verb}\n"

    def _serve(self) -> None:
        conn, _ = self.listener.accept()
        buffer = b""
        conn.settimeout(0.2)
        while not self._stop.is_set():
            try:
                data = conn.recv(4096)
            except TimeoutError:
                continue
            except OSError:
                break
            if not data:
                break
            buffer += data
            while b"\n" in buffer:
                raw, buffer = buffer.split(b"\n", 1)
                line = raw.decode().strip()
                if not line:
                    continue
                self.received.append(line)
                reply = self._respond(line)
                if reply:
                    conn.sendall(reply.encode())
        conn.close()

    def stop(self) -> None:
        self._stop.set()
        self.listener.close()


@pytest.fixture
def fake():
    server = FakeFluidSynth()
    yield server
    server.stop()


@pytest.fixture
def client(fake):
    c = FluidClient("127.0.0.1", fake.port)
    c.connect(retries=3, delay=0.2)
    yield c
    if c.sock:
        c.sock.close()


# -- the regression this file exists for ----------------------------------

def test_switching_fonts_does_not_wait_out_a_timeout_per_command(client, fake, tmp_path):
    """Seventeen silent commands used to cost seventeen timeouts -- about
    nineteen seconds -- regardless of how big the soundfont was."""
    font = tmp_path / "Piano.sf2"
    font.write_bytes(b"x")

    start = time.monotonic()
    client.switch_font(font, load_first=True, load_timeout=30.0)
    elapsed = time.monotonic() - start

    assert elapsed < 3.0, f"switch_font took {elapsed:.1f}s; silent commands are stalling again"


def test_all_sixteen_channels_are_retargeted(client, fake, tmp_path):
    font = tmp_path / "Piano.sf2"
    font.write_bytes(b"x")

    font_id = client.switch_font(font, load_first=True, load_timeout=30.0)

    selected = {int(c.split()[1]) for c in fake.received if c.startswith("select ")}
    assert selected == set(range(16))
    assert all(f"select {ch} {font_id} " in " ".join(fake.received) for ch in (0, 15))


def test_notes_are_silenced_before_channels_are_retargeted(client, fake, tmp_path):
    """A reset after retargeting would leave notes hanging on the old font."""
    font = tmp_path / "Piano.sf2"
    font.write_bytes(b"x")

    client.switch_font(font, load_first=True, load_timeout=30.0)

    assert fake.received.index("reset") < min(
        i for i, c in enumerate(fake.received) if c.startswith("select ")
    )


# -- load/unload ordering --------------------------------------------------

def test_previous_font_is_unloaded_after_the_new_one_loads(client, fake, tmp_path):
    first, second = tmp_path / "One.sf2", tmp_path / "Two.sf2"
    first.write_bytes(b"x")
    second.write_bytes(b"x")

    first_id = client.switch_font(first, load_first=True, load_timeout=30.0)
    client.switch_font(second, load_first=True, load_timeout=30.0)

    assert list(fake.fonts.values()) == [str(second)]
    load_two = max(i for i, c in enumerate(fake.received) if c.startswith("load ") and "Two" in c)
    unload_one = max(i for i, c in enumerate(fake.received) if c == f"unload {first_id}")
    assert load_two < unload_one, "old font must survive until the new one is in"


def test_unload_first_frees_memory_before_loading(client, fake, tmp_path):
    first, second = tmp_path / "One.sf2", tmp_path / "Two.sf2"
    first.write_bytes(b"x")
    second.write_bytes(b"x")

    first_id = client.switch_font(first, load_first=True, load_timeout=30.0)
    fake.received.clear()
    client.switch_font(second, load_first=False, load_timeout=30.0)

    unload_one = fake.received.index(f"unload {first_id}")
    load_two = min(i for i, c in enumerate(fake.received) if c.startswith("load "))
    assert unload_one < load_two, "with load_first=False the old font must go first"


def test_font_id_is_parsed_from_the_load_response(client, fake, tmp_path):
    font = tmp_path / "Piano.sf2"
    font.write_bytes(b"x")

    font_id = client.switch_font(font, load_first=True, load_timeout=30.0)

    assert font_id in fake.fonts
    assert fake.fonts[font_id] == str(font)


def test_connecting_to_nothing_raises_rather_than_hanging(tmp_path):
    dead = socket.socket()
    dead.bind(("127.0.0.1", 0))
    port = dead.getsockname()[1]
    dead.close()

    with pytest.raises(FluidError, match="Could not reach"):
        FluidClient("127.0.0.1", port).connect(retries=2, delay=0.1)


# -- sustain pedal threshold ----------------------------------------------

def test_standard_threshold_leaves_the_router_untouched(client, fake):
    """64 is what MIDI specifies, so there is nothing to rewrite."""
    fake.received.clear()
    client.configure_sustain(64)
    assert not any(c.startswith("router") for c in fake.received)


def test_lower_threshold_makes_the_half_pedal_position_engage(client, fake):
    fake.received.clear()
    client.configure_sustain(56)

    assert "router_clear" in fake.received
    # At or above the threshold becomes full sustain...
    assert "router_par2 56 127 0 127" in fake.received
    # ...and everything below it is forced fully off.
    assert "router_par2 0 55 0 0" in fake.received
    # Both rules must be scoped to controller 64 alone.
    assert fake.received.count("router_par1 64 64 1 0") == 2


def test_every_event_type_is_readmitted_after_clearing(client, fake):
    """router_clear blocks *all* MIDI. Forget to re-admit an event type here
    and the piano goes silent, which is a far worse bug than the one being
    fixed."""
    fake.received.clear()
    client.configure_sustain(56)

    for kind in ("note", "pbend", "prog", "cpress", "kpress"):
        assert f"router_begin {kind}" in fake.received, f"{kind} events would be dropped"
    # Controllers other than 64 must still get through.
    assert "router_par1 0 63 1 0" in fake.received
    assert "router_par1 65 127 1 0" in fake.received


def test_rules_are_balanced(client, fake):
    fake.received.clear()
    client.configure_sustain(56)
    begins = [c for c in fake.received if c.startswith("router_begin")]
    ends = [c for c in fake.received if c == "router_end"]
    assert len(begins) == len(ends)


def test_threshold_is_clamped_to_a_usable_range(client, fake):
    fake.received.clear()
    client.configure_sustain(0)          # would otherwise build "0 -1" as a range
    assert "router_par2 0 0 0 0" in fake.received
    assert "router_par2 1 127 0 127" in fake.received


def test_configuring_sustain_is_idempotent(client, fake):
    """It reruns on every reconnect, so applying it twice must be harmless."""
    client.configure_sustain(56)
    first = [c for c in fake.received if c.startswith("router")]
    fake.received.clear()
    client.configure_sustain(56)
    second = [c for c in fake.received if c.startswith("router")]

    assert first == second
    assert second.count("router_clear") == 1
