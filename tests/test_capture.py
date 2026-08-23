"""Tests for the rolling MIDI capture service.

Everything here runs without a Sense HAT, without FluidSynth and without a
MIDI interface: the ring buffer, the file format, the retention sweep and the
save socket are all exercised directly.
"""

import shutil
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

import mido
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import midi_capture as mc
from piano_control import CaptureClient

NOTE_ON = bytes([0x90, 60, 100])
NOTE_OFF = bytes([0x80, 60, 0])
SUSTAIN_DOWN = bytes([0xB0, 64, 127])
CLOCK = bytes([0xF8])
ACTIVE_SENSING = bytes([0xFE])


# -- filtering -------------------------------------------------------------

@pytest.mark.parametrize("data", [NOTE_ON, NOTE_OFF, SUSTAIN_DOWN,
                                 bytes([0xC0, 5]), bytes([0xE0, 0, 64])])
def test_musical_messages_are_kept(data):
    assert mc.is_musical(data)


@pytest.mark.parametrize("data", [CLOCK, ACTIVE_SENSING, bytes([0xFA]),
                                  bytes([0xF0, 0x7E]), b""])
def test_system_messages_are_dropped(data):
    assert not mc.is_musical(data)


def test_sustain_pedal_survives_filtering():
    """CC64 is the pedal. Dropping it would make every recording sound wrong."""
    assert mc.is_musical(SUSTAIN_DOWN)


# -- ring buffer -----------------------------------------------------------

def test_buffer_drops_events_outside_the_time_window():
    buffer = mc.RingBuffer(window_seconds=10, max_events=1000)
    buffer.append(100.0, NOTE_ON)
    buffer.append(105.0, NOTE_ON)
    buffer.append(115.0, NOTE_ON)          # pushes the cutoff to 105.0
    assert [t for t, _ in buffer.snapshot()] == [105.0, 115.0]


def test_buffer_keeps_everything_inside_the_window():
    buffer = mc.RingBuffer(window_seconds=3600, max_events=1000)
    for i in range(100):
        buffer.append(float(i), NOTE_ON)
    assert len(buffer) == 100


def test_max_events_is_a_hard_ceiling():
    """The window is the working policy; this is the anti-OOM backstop."""
    buffer = mc.RingBuffer(window_seconds=86_400, max_events=10)
    for i in range(50):
        buffer.append(float(i), NOTE_ON)
    assert len(buffer) == 10
    assert buffer.snapshot()[0][0] == 40.0


def test_snapshot_is_a_copy():
    buffer = mc.RingBuffer(window_seconds=3600, max_events=100)
    buffer.append(1.0, NOTE_ON)
    snapshot = buffer.snapshot()
    buffer.append(2.0, NOTE_OFF)
    assert len(snapshot) == 1


def test_concurrent_appends_do_not_corrupt_a_snapshot():
    """The rtmidi callback runs on its own thread while a save is snapshotting."""
    buffer = mc.RingBuffer(window_seconds=3600, max_events=100_000)
    stop = threading.Event()

    def writer():
        i = 0
        while not stop.is_set():
            buffer.append(time.monotonic(), NOTE_ON)
            i += 1

    thread = threading.Thread(target=writer, daemon=True)
    thread.start()
    try:
        for _ in range(200):
            assert all(len(d) == 3 for _, d in buffer.snapshot())
    finally:
        stop.set()
        thread.join(timeout=2)


# -- MIDI file output ------------------------------------------------------

def test_empty_buffer_still_produces_a_valid_file():
    midi_file = mc.events_to_midifile([])
    assert len(midi_file.tracks) == 1


def test_events_become_messages_with_real_timing():
    events = [
        (10.0, NOTE_ON),
        (10.5, NOTE_OFF),
        (11.5, NOTE_ON),
    ]
    track = mc.events_to_midifile(events).tracks[0]
    notes = [m for m in track if not m.is_meta]

    assert [m.type for m in notes] == ["note_on", "note_off", "note_on"]
    # 480 ticks/beat at 120bpm = 960 ticks/second.
    assert notes[0].time == 0
    assert notes[1].time == pytest.approx(480, abs=1)
    assert notes[2].time == pytest.approx(960, abs=1)


def test_tempo_is_written_so_playback_speed_matches_performance():
    track = mc.events_to_midifile([(0.0, NOTE_ON)]).tracks[0]
    tempos = [m for m in track if m.is_meta and m.type == "set_tempo"]
    assert tempos and tempos[0].tempo == mc.TEMPO_US_PER_BEAT


def test_pedal_is_preserved_in_the_written_file(tmp_path):
    events = [(0.0, NOTE_ON), (0.1, SUSTAIN_DOWN), (1.0, NOTE_OFF)]
    target = tmp_path / "out.mid"
    mc.events_to_midifile(events).save(str(target))

    reloaded = mido.MidiFile(str(target))
    controls = [m for m in reloaded if m.type == "control_change"]
    assert len(controls) == 1
    assert controls[0].control == 64 and controls[0].value == 127


def test_undecodable_events_are_skipped_not_fatal():
    events = [(0.0, NOTE_ON), (0.1, bytes([0x90])), (0.2, NOTE_OFF)]
    track = mc.events_to_midifile(events).tracks[0]
    assert len([m for m in track if not m.is_meta]) == 2


def test_filename_has_second_resolution():
    """Saves are non-destructive, so two in the same minute must not collide."""
    import datetime

    moment = datetime.datetime(2026, 8, 23, 14, 32, 7)
    assert mc.timestamped_name(moment) == "2026-08-23_14-32-07.mid"


# -- retention -------------------------------------------------------------

def _aged(path: Path, days: float):
    old = time.time() - days * mc.SECONDS_PER_DAY
    path.write_bytes(b"x")
    import os

    os.utime(path, (old, old))


def test_recordings_past_the_window_are_deleted(tmp_path):
    _aged(tmp_path / "old.mid", 40)
    _aged(tmp_path / "fresh.mid", 3)

    removed = mc.prune_recordings(tmp_path, retention_days=30)

    assert [p.name for p in removed] == ["old.mid"]
    assert not (tmp_path / "old.mid").exists()
    assert (tmp_path / "fresh.mid").exists()


def test_zero_retention_disables_deletion_entirely(tmp_path):
    _aged(tmp_path / "ancient.mid", 5000)
    assert mc.prune_recordings(tmp_path, retention_days=0) == []
    assert (tmp_path / "ancient.mid").exists()


def test_pruning_only_touches_mid_files(tmp_path):
    _aged(tmp_path / "old.mid", 90)
    _aged(tmp_path / "notes.txt", 90)
    _aged(tmp_path / "recording.wav", 90)

    mc.prune_recordings(tmp_path, retention_days=30)

    assert not (tmp_path / "old.mid").exists()
    assert (tmp_path / "notes.txt").exists()
    assert (tmp_path / "recording.wav").exists()


def test_pruning_does_not_recurse_into_subdirectories(tmp_path):
    keep = tmp_path / "archive"
    keep.mkdir()
    _aged(keep / "precious.mid", 400)

    mc.prune_recordings(tmp_path, retention_days=30)

    assert (keep / "precious.mid").exists()


def test_pruning_ignores_a_directory_named_like_a_recording(tmp_path):
    (tmp_path / "weird.mid").mkdir()
    assert mc.prune_recordings(tmp_path, retention_days=30) == []
    assert (tmp_path / "weird.mid").is_dir()


# -- save, end to end over the socket --------------------------------------

@pytest.fixture
def short_socket_dir():
    """AF_UNIX paths are capped near 104 characters, and pytest's tmp_path is
    long enough on macOS to blow through that. The socket needs a short home."""
    directory = tempfile.mkdtemp(prefix="pcap")
    try:
        yield Path(directory)
    finally:
        shutil.rmtree(directory, ignore_errors=True)


@pytest.fixture
def running_capture(tmp_path, short_socket_dir):
    config = mc.CaptureConfig({
        "capture": {
            "socket_path": str(short_socket_dir / "capture.sock"),
            "recordings_dir": str(tmp_path / "recordings"),
            "window_minutes": 60,
            "max_events": 1000,
            "retention_days": 30,
        }
    })
    buffer = mc.RingBuffer(config.window_seconds, config.max_events)
    server = mc.ControlServer(config.socket_path, mc.Recorder(buffer, config))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield config, buffer
    finally:
        server.shutdown()
        server.server_close()


def test_save_writes_a_file_and_reports_its_name(running_capture):
    config, buffer = running_capture
    buffer.append(0.0, NOTE_ON)
    buffer.append(1.0, NOTE_OFF)

    name = CaptureClient(config.socket_path).save()

    written = config.recordings_dir / name
    assert written.exists()
    assert len([m for m in mido.MidiFile(str(written)) if m.type.startswith("note")]) == 2


def test_saving_does_not_empty_the_buffer(running_capture):
    """Non-destructive by design: holding twice gives two overlapping files."""
    config, buffer = running_capture
    buffer.append(0.0, NOTE_ON)

    first = CaptureClient(config.socket_path).save()
    assert len(buffer) == 1
    second = CaptureClient(config.socket_path).save()

    assert (config.recordings_dir / first).exists()
    assert (config.recordings_dir / second).exists()


def test_saving_an_empty_buffer_reports_an_error(running_capture):
    config, _ = running_capture
    with pytest.raises(RuntimeError, match="empty"):
        CaptureClient(config.socket_path).save()


def test_save_prunes_old_recordings(running_capture):
    config, buffer = running_capture
    config.recordings_dir.mkdir(parents=True, exist_ok=True)
    _aged(config.recordings_dir / "2020-01-01_00-00-00.mid", 400)
    buffer.append(0.0, NOTE_ON)

    CaptureClient(config.socket_path).save()

    assert not (config.recordings_dir / "2020-01-01_00-00-00.mid").exists()


def test_unknown_commands_are_rejected(running_capture):
    config, _ = running_capture
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.connect(str(config.socket_path))
        sock.sendall(b"rm -rf\n")
        assert sock.recv(64).startswith(b"ERR")


def test_client_raises_when_capture_is_not_running(tmp_path):
    with pytest.raises(OSError):
        CaptureClient(tmp_path / "nothing.sock", timeout=1).save()


# -- port selection --------------------------------------------------------

def test_midi_through_is_never_chosen():
    """ALSA's virtual loopback matches most substrings and carries no data."""
    config = mc.CaptureConfig({"capture": {"port_match": "MIDI"}})
    source = mc.MidiSource(mc.RingBuffer(60, 10), config)
    assert source._choose_port(["Midi Through:0", "USB MIDI Interface:0"]) == 1


def test_first_real_port_used_when_nothing_matches():
    config = mc.CaptureConfig({"capture": {"port_match": "nonexistent"}})
    source = mc.MidiSource(mc.RingBuffer(60, 10), config)
    assert source._choose_port(["Midi Through:0", "Some Keyboard:0"]) == 1


def test_no_port_when_only_loopback_present():
    config = mc.CaptureConfig({"capture": {"port_match": "MIDI"}})
    source = mc.MidiSource(mc.RingBuffer(60, 10), config)
    assert source._choose_port(["Midi Through:0"]) is None
