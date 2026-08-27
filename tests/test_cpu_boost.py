"""Tests for the CPU governor boost.

The behaviour that matters is hysteresis: any note raises the clock, and it
stays raised through quiet passages until you have genuinely stopped. No stock
governor does that -- `ondemand` and `schedutil` react to instantaneous load,
so a quiet bar mid-piece looks identical to having gone to bed.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cpu_boost import BoostConfig, BoostController

NOTE = bytes([0x90, 60, 100])


class FakeGovernor:
    """Records what would have been asked of systemd."""

    def __init__(self, fails=False):
        self.calls = []
        self.fails = fails

    def boost(self):
        self.calls.append("boost")
        return not self.fails

    def release(self):
        self.calls.append("release")
        return not self.fails


@pytest.fixture
def controller():
    return BoostController(FakeGovernor(), idle_release=180.0)


# -- the core behaviour ----------------------------------------------------

def test_a_note_raises_the_governor(controller):
    controller.on_event(1000.0, NOTE)
    controller.step(1000.0)

    assert controller.governor.calls == ["boost"]
    assert controller.boosted


def test_it_stays_raised_through_a_quiet_passage(controller):
    """The whole point. Two minutes of near-silence mid-session must not drop
    the clock, or the next dense chord crackles."""
    controller.on_event(1000.0, NOTE)
    controller.step(1000.0)
    controller.governor.calls.clear()

    for t in (1030.0, 1060.0, 1120.0, 1179.0):
        controller.step(t)

    assert controller.governor.calls == [], "governor was disturbed mid-session"
    assert controller.boosted


def test_it_releases_once_genuinely_idle(controller):
    controller.on_event(1000.0, NOTE)
    controller.step(1000.0)
    controller.governor.calls.clear()

    controller.step(1000.0 + 180.0 + 0.1)

    assert controller.governor.calls == ["release"]
    assert not controller.boosted


def test_playing_again_resets_the_idle_clock(controller):
    controller.on_event(1000.0, NOTE)
    controller.step(1000.0)
    controller.on_event(1170.0, NOTE)          # just before the timeout
    controller.governor.calls.clear()

    controller.step(1179.0)                    # 179s after the first note
    assert controller.governor.calls == []
    controller.step(1349.0)                    # 179s after the second
    assert controller.governor.calls == []
    controller.step(1351.0)                    # now past it
    assert controller.governor.calls == ["release"]


# -- not doing needless work -----------------------------------------------

def test_further_notes_do_not_re_boost(controller):
    for t in (1000.0, 1001.0, 1002.0):
        controller.on_event(t, NOTE)
        controller.step(t)

    assert controller.governor.calls == ["boost"], "systemctl called more than once"


def test_idle_at_startup_does_nothing(controller):
    """Nothing has been played, so there is nothing to release."""
    controller.step(1000.0)
    controller.step(9999.0)

    assert controller.governor.calls == []
    assert not controller.boosted


# -- failure handling ------------------------------------------------------

def test_a_failed_boost_is_retried_rather_than_latched():
    """Usually a missing sudoers rule. It should keep trying rather than
    silently deciding it is boosted."""
    c = BoostController(FakeGovernor(fails=True), idle_release=180.0)
    c.on_event(1000.0, NOTE)
    c.step(1000.0)
    c.step(1001.0)

    assert c.governor.calls == ["boost", "boost"]
    assert not c.boosted


def test_the_callback_never_blocks(controller):
    """It runs on rtmidi's delivery thread, so it must only stamp a time --
    systemctl takes ~100ms and would stall MIDI delivery."""
    controller.on_event(1000.0, NOTE)

    assert controller.governor.calls == [], "callback actuated the governor directly"
    assert controller.last_activity == 1000.0


# -- config ----------------------------------------------------------------

def test_disabled_by_config():
    assert BoostConfig({"performance": {"boost_enabled": False}}).enabled is False


def test_port_match_is_shared_with_the_other_services():
    cfg = BoostConfig({"midi": {"port_match": "Yamaha"}})
    assert cfg.port_match == "Yamaha"


def test_defaults_are_sane():
    cfg = BoostConfig({})
    assert cfg.enabled is True
    assert cfg.idle_release == 180
    assert cfg.port_match == "MIDI"
