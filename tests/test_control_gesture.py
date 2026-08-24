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

    def clear(self):
        pass

    def set_pixels(self, frame):
        pass

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

    def _build(script, hold_seconds=0.1):
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
        })
        sense = FakeSense(FakeStick(script))
        fluid = FakeFluid()
        capture = FakeCapture()
        app = App(config, sense, fluid, capture)
        return app, fluid, capture

    return _build


def run(app):
    with pytest.raises(StopLoop):
        app.run()


# -- tap -------------------------------------------------------------------

def test_a_tap_still_loads_the_font_under_the_cursor(build):
    """The pre-existing behaviour must survive the move to release."""
    app, fluid, capture = build([
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
    app, fluid, capture = build([[event("middle", "pressed")]] , hold_seconds=99)
    run(app)

    assert fluid.loaded == ["Alpha.sf2"]      # startup only, no tap action
    assert capture.calls == 0


# -- hold ------------------------------------------------------------------

def test_holding_saves_a_recording(build):
    app, fluid, capture = build([[event("middle", "pressed")]])
    run(app)

    assert capture.calls == 1


def test_holding_does_not_also_load(build):
    """The release that ends a hold must not fall through to the tap action."""
    app, fluid, capture = build([
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
    app, fluid, capture = build([[event("middle", "pressed")]])
    run(app)

    assert capture.calls == 1


def test_saving_works_while_the_display_is_asleep(build):
    """You will be mid-playing and not looking at the HAT when you reach for
    this, so it must not require waking the display first."""
    app, fluid, capture = build([[event("middle", "pressed")]])
    assert app.awake is False
    run(app)

    assert capture.calls == 1
    assert app.awake is False


def test_a_failed_save_does_not_kill_the_app(build, tmp_path):
    app, fluid, capture = build([[event("middle", "pressed")]])
    app.capture = FakeCapture(error=OSError("capture service is down"))
    run(app)                                   # StopLoop, not OSError

    assert fluid.loaded == ["Alpha.sf2"]


def test_hold_is_inert_when_capture_is_disabled(build):
    app, fluid, _ = build([[event("middle", "pressed")]])
    app.capture = None
    run(app)

    assert fluid.loaded == ["Alpha.sf2"]


# -- direction buttons are untouched ---------------------------------------

def test_direction_presses_still_act_immediately(build):
    app, fluid, capture = build([
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
