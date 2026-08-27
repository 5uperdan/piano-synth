"""Tests for the shared MIDI tap.

Both piano-capture and piano-boost subscribe through this, so a fault here
breaks recording and the CPU boost together.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from midi_source import MidiSource, is_musical

NOTE_ON = bytes([0x90, 60, 100])
NOTE_OFF = bytes([0x80, 60, 0])
SUSTAIN = bytes([0xB0, 64, 127])
CLOCK = bytes([0xF8])
ACTIVE_SENSING = bytes([0xFE])


def tap(port_match="MIDI"):
    return MidiSource(lambda _t, _d: None, port_match)


# -- filtering -------------------------------------------------------------

@pytest.mark.parametrize("data", [NOTE_ON, NOTE_OFF, SUSTAIN,
                                  bytes([0xC0, 5]), bytes([0xE0, 0, 64])])
def test_musical_messages_are_kept(data):
    assert is_musical(data)


@pytest.mark.parametrize("data", [CLOCK, ACTIVE_SENSING, bytes([0xFA]),
                                  bytes([0xF0, 0x7E]), b""])
def test_system_messages_are_dropped(data):
    assert not is_musical(data)


def test_sustain_pedal_survives_filtering():
    """CC64 is the pedal. Dropping it would make every recording sound wrong."""
    assert is_musical(SUSTAIN)


# -- port selection --------------------------------------------------------

def test_midi_through_is_never_chosen():
    """ALSA's virtual loopback contains "midi", so a substring match alone
    would select it -- and it is listed first, so this is not hypothetical."""
    assert tap()._choose_port(["Midi Through:0", "USB MIDI Interface:0"]) == 1


def test_first_real_port_used_when_nothing_matches():
    assert tap("nonexistent")._choose_port(["Midi Through:0", "Some Keyboard:0"]) == 1


def test_no_port_when_only_loopback_present():
    assert tap()._choose_port(["Midi Through:0"]) is None


# -- the callback contract -------------------------------------------------

def test_only_musical_events_reach_the_callback():
    seen = []
    source = MidiSource(lambda t, d: seen.append(d), "MIDI")
    for payload in (NOTE_ON, CLOCK, SUSTAIN, ACTIVE_SENSING, NOTE_OFF):
        source._on_message((list(payload), 0.0))

    assert seen == [NOTE_ON, SUSTAIN, NOTE_OFF]


def test_the_callback_receives_a_monotonic_timestamp():
    stamps = []
    source = MidiSource(lambda t, d: stamps.append(t), "MIDI")
    source._on_message((list(NOTE_ON), 0.0))
    source._on_message((list(NOTE_OFF), 0.0))

    assert len(stamps) == 2
    assert stamps[1] >= stamps[0]
