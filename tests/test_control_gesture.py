"""Tests for the middle-button gesture split in piano_control.py.

Adding hold-to-save meant the middle button had to start acting on release
rather than on press, so that a hold could be told apart from a tap before
either action committed. That is the one change this feature makes to code
that already worked, so it gets its own tests.

The App loop is driven directly with a scripted joystick and stand-ins for
FluidSynth, the capture service and the Sense HAT.
"""

import sys
import threading
import time
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import piano_control
from piano_control import App, Config, Matrix


class StopLoop(Exception):
    """Breaks out of App.run(), which is otherwise an infinite loop."""


def event(direction, action):
    return types.SimpleNamespace(direction=direction, action=action)


class FakeStick:
    def __init__(self, script, max_idle_polls=30):
        # One entry per poll of the joystick. Leading [] absorbs the drain
        # App.run() performs before entering its loop.
        self.script = [[]] + list(script)
        self.idle_polls = 0
        self.max_idle_polls = max_idle_polls

    def get_events(self):
        if self.script:
            return self.script.pop(0)
        self.idle_polls += 1
        if self.idle_polls > self.max_idle_polls:
            raise StopLoop
        return []


class FakeSense:
    def __init__(self, stick):
        self.stick = stick
        self.low_light = True
        self.lit_counts = []          # non-black pixels per frame drawn

    def clear(self):
        self.lit_counts.append(0)

    def set_pixels(self, frame):
        self.lit_counts.append(sum(1 for p in frame if p != (0, 0, 0)))

    def set_rotation(self, rotation):
        pass


class FakeFluid:
    def __init__(self):
        self.loaded = []

    def switch_font(self, path, load_first, load_timeout):
        self.loaded.append(Path(path).name)
        return len(self.loaded)


class FakeCapture:
    def __init__(self, result="2026-08-23_14-32-07.mid", error=None):
        self.result = result
        self.error = error
        self.calls = 0

    def save(self):
        self.calls += 1
        if self.error:
            raise self.error
        return self.result


@pytest.fixture
def build(tmp_path):
    fonts = tmp_path / "soundfonts"
    fonts.mkdir()
    (fonts / "Alpha.sf2").write_bytes(b"x")
    (fonts / "Beta.sf2").write_bytes(b"x")

    def _build(script, hold_seconds=0.1, shutdown_seconds=0.2, shutdown=True,
               output_seconds=0.2, output=True):
        config = Config({
            "paths": {
                "soundfont_dir": str(fonts),
                "state_file": str(tmp_path / "state.json"),
            },
            "display": {
                "show_ready_animation": False,
                "highlight_seconds": 0.01,
                "scroll_step_seconds": 0.001,
            },
            "capture": {"hold_seconds": hold_seconds},
            "shutdown": {
                "enabled": shutdown,
                "hold_direction": "down",
                "hold_seconds": shutdown_seconds,
            },
            "output": {
                "toggle_enabled": output,
                "hold_direction": "up",
                "hold_seconds": output_seconds,
                # Never the real one -- tests must not touch a home directory.
                "state_file": str(tmp_path / "output.env"),
            },
        })
        sense = FakeSense(FakeStick(script))
        fluid = FakeFluid()
        capture = FakeCapture()
        app = App(config, sense, fluid, capture)
        return app, fluid, capture, sense

    return _build


def run(app):
    with pytest.raises(StopLoop):
        app.run()


# -- tap -------------------------------------------------------------------

def test_a_tap_still_loads_the_font_under_the_cursor(build):
    """The pre-existing behaviour must survive the move to release."""
    app, fluid, capture, _sense = build([
        [event("right", "pressed")],       # wake
        [event("right", "pressed")],       # move to Beta
        [event("middle", "pressed")],
        [event("middle", "released")],
    ])
    run(app)

    # Alpha at startup, then Beta from the tap.
    assert fluid.loaded == ["Alpha.sf2", "Beta.sf2"]
    assert capture.calls == 0


def test_nothing_loads_until_the_button_is_released(build):
    app, fluid, capture, _sense = build([[event("middle", "pressed")]] , hold_seconds=99)
    run(app)

    assert fluid.loaded == ["Alpha.sf2"]      # startup only, no tap action
    assert capture.calls == 0


# -- hold ------------------------------------------------------------------

def test_holding_saves_a_recording(build):
    app, fluid, capture, _sense = build([[event("middle", "pressed")]])
    run(app)

    assert capture.calls == 1


def test_holding_does_not_also_load(build):
    """The release that ends a hold must not fall through to the tap action."""
    app, fluid, capture, _sense = build([
        [event("right", "pressed")],       # wake
        [event("right", "pressed")],       # move onto Beta
        [event("middle", "pressed")],
        [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [],
        [event("middle", "released")],
    ])
    run(app)

    assert capture.calls == 1
    assert fluid.loaded == ["Alpha.sf2"]      # Beta never loaded


def test_a_hold_saves_exactly_once(build):
    app, fluid, capture, _sense = build([[event("middle", "pressed")]])
    run(app)

    assert capture.calls == 1


def test_saving_works_while_the_display_is_asleep(build):
    """You will be mid-playing and not looking at the HAT when you reach for
    this, so it must not require waking the display first."""
    app, fluid, capture, _sense = build([[event("middle", "pressed")]])
    assert app.awake is False
    run(app)

    assert capture.calls == 1
    assert app.awake is False


def test_a_failed_save_does_not_kill_the_app(build, tmp_path):
    app, fluid, capture, _sense = build([[event("middle", "pressed")]])
    app.capture = FakeCapture(error=OSError("capture service is down"))
    run(app)                                   # StopLoop, not OSError

    assert fluid.loaded == ["Alpha.sf2"]


def test_hold_is_inert_when_capture_is_disabled(build):
    app, fluid, _capture, _sense = build([[event("middle", "pressed")]])
    app.capture = None
    run(app)

    assert fluid.loaded == ["Alpha.sf2"]


# -- direction buttons are untouched ---------------------------------------

def test_direction_presses_still_act_immediately(build):
    app, fluid, capture, _sense = build([
        [event("right", "pressed")],
        [event("right", "pressed")],
    ])
    run(app)

    assert app.cursor == 1
    assert capture.calls == 0


# -- animation cancellation ------------------------------------------------

@pytest.fixture
def matrix():
    return Matrix(FakeSense(FakeStick([])), Config({}))


def test_sleep_reports_running_to_completion(matrix):
    assert matrix.sleep(threading.Event(), 0.02) is True


def test_sleep_reports_an_already_cancelled_token(matrix):
    token = threading.Event()
    token.set()
    assert matrix.sleep(token, 5.0) is False


def test_sleep_returns_the_moment_it_is_cancelled(matrix):
    """Inputs must interrupt an animation immediately. If this ever blocks for
    the full duration, "any input cancels the current behaviour" is broken."""
    token = threading.Event()
    threading.Timer(0.05, token.set).start()

    start = time.monotonic()
    assert matrix.sleep(token, 5.0) is False
    assert time.monotonic() - start < 1.0


# -- shutdown countdown ----------------------------------------------------

@pytest.fixture
def halted(monkeypatch):
    """Records poweroff calls instead of actually halting the machine."""
    calls = []
    monkeypatch.setattr(piano_control, "poweroff", lambda: (calls.append(1), True)[1])
    return calls


def test_holding_down_to_a_full_grid_powers_off(build, halted):
    app, fluid, capture, sense = build([[event("down", "pressed")]])
    run(app)

    assert len(halted) == 1


def test_releasing_early_aborts(build, halted):
    """The entire safety mechanism: you can start one and let go."""
    app, fluid, capture, sense = build(
        [[event("down", "pressed")], [], [], [event("down", "released")]],
        shutdown_seconds=5.0,
    )
    run(app)

    assert halted == []


def test_the_grid_fills_progressively(build, halted):
    app, fluid, capture, sense = build([[event("down", "pressed")]])
    run(app)

    counts = sense.lit_counts
    # The grid should pass through partial fills rather than jumping from
    # blank straight to done. (Monotonicity is asserted against
    # shutdown_progress directly; these frames are interleaved with the
    # cursor animation the same key press starts, so ordering here is noisy.)
    assert any(0 < c < 64 for c in counts), "countdown never showed an intermediate state"
    assert max(counts) == 64, "countdown never completed"


def test_the_display_is_cleared_when_the_hold_is_abandoned(build, halted):
    app, fluid, capture, sense = build(
        [[event("down", "pressed")], [], [], [event("down", "released")]],
        shutdown_seconds=5.0,
    )
    run(app)

    assert sense.lit_counts[-1] == 0


def test_directions_without_a_gesture_do_nothing(build, halted, restarts):
    """left and right only move the cursor; only up and down are gestures."""
    app, fluid, capture, sense = build([[event("right", "pressed")]])
    run(app)

    assert halted == []
    assert restarts == []
    assert max(sense.lit_counts) < 64


def test_disabling_it_makes_the_gesture_inert(build, halted):
    app, fluid, capture, sense = build([[event("down", "pressed")]], shutdown=False)
    run(app)

    assert halted == []


def test_a_refused_shutdown_does_not_kill_the_app(build, monkeypatch):
    """No sudoers rule means poweroff fails. That must flash and carry on."""
    monkeypatch.setattr(piano_control, "poweroff", lambda: False)
    app, fluid, capture, sense = build([[event("down", "pressed")]])

    run(app)                                   # StopLoop, not an exception

    assert fluid.loaded == ["Alpha.sf2"]       # still alive and serving


def test_progress_is_monotonic_and_bounded():
    from piano_control import HoldGesture

    config = Config({})
    app = App(config, FakeSense(FakeStick([])), FakeFluid(), FakeCapture())
    gesture = HoldGesture("down", 5.0, (1, 2, 3), lambda: None)

    assert app.hold_progress(gesture, 0.0) == 0
    assert app.hold_progress(gesture, 2.5) == 32
    assert app.hold_progress(gesture, 5.0) == 64
    assert app.hold_progress(gesture, 60.0) == 64   # clamped, never overruns


# -- shutdown signal handling ----------------------------------------------

def test_sigterm_becomes_systemexit_so_cleanup_runs():
    """systemd stops services with SIGTERM. Python's default disposition kills
    the process outright, so `finally` never runs and the Sense HAT keeps
    displaying whatever was last written -- indefinitely, since a halted Pi
    still powers the HAT. This is what makes the display clear on shutdown."""
    import os
    import signal as signal_module

    previous = signal_module.getsignal(signal_module.SIGTERM)
    try:
        piano_control.exit_on_sigterm()
        with pytest.raises(SystemExit):
            os.kill(os.getpid(), signal_module.SIGTERM)
            time.sleep(0.2)          # give the handler a chance to run
    finally:
        signal_module.signal(signal_module.SIGTERM, previous)


def test_the_handler_is_not_the_default():
    import signal as signal_module

    previous = signal_module.getsignal(signal_module.SIGTERM)
    try:
        piano_control.exit_on_sigterm()
        assert signal_module.getsignal(signal_module.SIGTERM) is not signal_module.SIG_DFL
    finally:
        signal_module.signal(signal_module.SIGTERM, previous)


# -- mono / stereo output toggle -------------------------------------------

@pytest.fixture
def restarts(monkeypatch):
    """Records FluidSynth restarts instead of performing them."""
    calls = []
    monkeypatch.setattr(piano_control, "restart_fluidsynth",
                        lambda: (calls.append(1), True)[1])
    return calls


def test_holding_up_switches_to_mono(build, restarts):
    app, fluid, capture, sense = build([[event("up", "pressed")]])
    assert app.output_is_mono() is False
    run(app)

    assert app.output_is_mono() is True
    assert app.config.mono_device in app.config.output_state_file.read_text()
    assert len(restarts) == 1


def test_holding_up_again_switches_back_to_stereo(build, restarts):
    app, fluid, capture, sense = build([[event("up", "pressed")]])
    app.toggle_output()                       # now mono
    assert app.output_is_mono() is True
    restarts.clear()

    app.toggle_output()                       # back to stereo

    assert app.output_is_mono() is False
    assert not app.config.output_state_file.exists()
    assert len(restarts) == 1


def test_releasing_early_does_not_switch(build, restarts):
    app, fluid, capture, sense = build(
        [[event("up", "pressed")], [], [], [event("up", "released")]],
        shutdown_seconds=5.0, output_seconds=5.0)
    run(app)

    assert app.output_is_mono() is False
    assert restarts == []


def test_the_state_is_announced_before_the_restart(build, monkeypatch):
    """The restart may stop this service, so the message must already be done."""
    order = []
    monkeypatch.setattr(piano_control, "restart_fluidsynth",
                        lambda: (order.append("restart"), True)[1])
    app, fluid, capture, sense = build([[event("up", "pressed")]])
    monkeypatch.setattr(app, "announce_output", lambda mono: order.append("announce"))

    run(app)

    assert order == ["announce", "restart"]


def test_an_abandoned_hold_still_reports_the_current_state(build, restarts):
    """Letting go tells you where you are, rather than leaving you guessing."""
    announced = []
    app, fluid, capture, sense = build(
        # Long enough not to complete, short enough that the fill starts.
        [[event("up", "pressed")]] + [[]] * 12 + [[event("up", "released")]],
        shutdown_seconds=5.0, output_seconds=1.0)
    app.announce_output = lambda mono: announced.append(mono)

    run(app)

    assert announced == [False], "cancelling should have reported stereo"
    assert restarts == []


def test_a_failed_restart_does_not_kill_the_app(build, monkeypatch):
    monkeypatch.setattr(piano_control, "restart_fluidsynth", lambda: False)
    app, fluid, capture, sense = build([[event("up", "pressed")]])

    run(app)                                   # StopLoop, not an exception

    assert app.output_is_mono() is True         # state still recorded


def test_disabling_the_toggle_makes_up_inert(build, restarts):
    app, fluid, capture, sense = build([[event("up", "pressed")]], output=False)
    run(app)

    assert restarts == []
    assert app.output_is_mono() is False


# -- the two gestures stay distinct ----------------------------------------

def test_down_still_shuts_down_and_does_not_touch_output(build, halted, restarts):
    app, fluid, capture, sense = build([[event("down", "pressed")]])
    run(app)

    assert len(halted) == 1
    assert restarts == []
    assert app.output_is_mono() is False


def test_up_does_not_shut_down(build, halted, restarts):
    app, fluid, capture, sense = build([[event("up", "pressed")]])
    run(app)

    assert halted == []
    assert len(restarts) == 1


def test_the_two_gestures_use_different_colours(build):
    app, fluid, capture, sense = build([])
    by_direction = {g.direction: g.colour for g in app.holds}

    assert by_direction["down"] != by_direction["up"]
    assert by_direction["down"] == app.config.colour_error
    assert by_direction["up"] == app.config.colour_output
