"""python -m keypadstudio.selftest  -- no keypad, no Home Assistant, no input sent."""

import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "sdcx-keypad"))

from keypadstudio import config, engine, win  # noqa: E402


class FakeEngine(engine.Engine):
    def __init__(self, **settings):
        cfg = copy.deepcopy(config.DEFAULT)
        cfg["settings"].update(settings)
        super().__init__(cfg)
        self.ran, self.knobbed, self.switched = [], [], []

    def _run(self, a):
        self.ran.append(a.get("label", a["type"]))

    def _knob(self, units):
        self.knobbed.append(units)

    def save(self):
        pass

    def activate(self, pid, auto=False):
        self.cfg["active"] = pid
        self.switched.append(pid)


def press(e, i, t0, t1):
    e._fire(t0); e._handle(i, True, t0); e._fire(t1); e._handle(i, False, t1)


def test_keys():
    e = FakeEngine()
    e.profile()["keys"][0]["tap"]["label"] = "tap0"
    press(e, 0, 0.0, 0.1)
    assert e.ran == ["tap0"], e.ran
    e._handle(0, True, 1.0); e._handle(0, True, 1.05)  # auto-repeat ignored
    assert e.ran == ["tap0", "tap0"]
    e._handle(0, False, 1.1)
    # with a hold action: short press = tap on release, long press = hold only
    e.ran.clear()
    e.profile()["keys"][1]["hold"] = {"type": "media", "label": "hold1"}
    e.profile()["keys"][1]["tap"]["label"] = "tap1"
    e._handle(1, True, 2.0); assert e.ran == []
    e._fire(2.2); e._handle(1, False, 2.2)
    assert e.ran == ["tap1"], e.ran
    press(e, 1, 3.0, 3.6)
    assert e.ran == ["tap1", "hold1"], e.ran


def test_knob_press():
    e = FakeEngine(longPress="none")
    press(e, 6, 0, 0.1)
    assert e.switched == ["pc"]
    press(e, 6, 1, 1.1)
    assert e.switched == ["pc", "desk"]  # wraps around
    d = FakeEngine(dblPrev=True, longPress="none")
    press(d, 6, 0, 0.05); d._fire(0.2); assert d.switched == []
    press(d, 6, 0.25, 0.3)               # second press inside 0.3 s
    d._fire(1.0)
    assert d.switched == ["pc"] and d.cfg["active"] == "pc", d.switched  # cycle(-1) from desk, 2 profiles
    press(d, 6, 2.0, 2.05); d._fire(2.5)
    assert d.switched == ["pc", "desk"]  # single press, after the wait


def test_picker():
    e = FakeEngine(longPress="picker")
    e._handle(6, True, 0.0)
    e._handle(7, True, 0.1); e._handle(7, False, 0.12)   # turn while held -> pick, no knob action
    assert e.knobbed == [] and e.picking == "pc"
    e._fire(0.2); e._handle(6, False, 0.2)
    assert e.switched == ["pc"] and e.picking is None
    e._handle(6, True, 1.0); e._fire(1.6); e._handle(6, False, 1.7)  # hold without turning: stays put
    assert e.switched == ["pc", "pc"]


def test_turn():
    e = FakeEngine()
    k = e.profile()["knob"]
    k.update(sens=2, accel=False, invert=True)
    for t in (0, 1, 2, 3):
        e._handle(7, True, t); e._handle(7, False, t)
    assert e.knobbed == [-1, -1], e.knobbed  # every 2nd click, reversed
    k.update(sens=1, accel=True, invert=False)
    e.knobbed.clear()
    for t in (10.0, 10.05, 10.15, 11.0):
        e._handle(8, True, t)
    assert e.knobbed == [-1, -3, -2, -1], e.knobbed


def test_flip():
    e = FakeEngine(flip=True)
    e.profile()["keys"][0]["tap"]["label"] = "top-left"
    press(e, 5, 0, 0.1)  # physical key 6 is top-left when the pad is turned around
    assert e.ran == ["top-left"], e.ran


def test_lighting():
    from keypadstudio.lighting import DEFAULT_AIR, EFFECTS, FLOOR, frame, levels, look
    p = {"color": "#102030", "led": "steady"}
    assert look(p, None)["color"] == "#102030"
    ac = {**p, "live": {"type": "climate", "entity": "climate.x"}}
    assert look(ac, {"state": "cool", "attributes": {"temperature": 24}})["fx"] == "wave"
    assert look(ac, {"state": "off", "attributes": {}})["color"] == "#102030"
    assert look(ac, {"state": "unavailable"})["fx"] == "dim"
    air = {**p, "live": {"type": "air"}}
    assert [look(air, {"state": v})["color"] for v in ("5", "20", "40", "300")] == [b["color"] for b in DEFAULT_AIR]
    lamp = {**p, "live": {"type": "light", "useColor": False}}
    assert abs(look(lamp, {"state": "on", "attributes": {"brightness": 128}})["level"] - 0.5) < 0.01
    assert look(lamp, {"state": "off"})["level"] == FLOOR
    for fx in EFFECTS:
        for t in (0, 0.37, 1.3, 7.9):
            ks = levels(fx, t)
            assert len(ks) == 6 and all(0 <= k <= 1 for k in ks), (fx, t, ks)
    assert frame((255, 0, 0), 0.5, "steady", 0) == [(128, 0, 0)] * 6
    # unique look for every AC mode
    from keypadstudio.lighting import DEFAULT_CLIMATE
    looks = [(r["color"], r["fx"]) for r in DEFAULT_CLIMATE.values()]
    assert len(set(looks)) == len(looks)


def test_paused():
    e = FakeEngine()
    e.paused = True
    press(e, 0, 0, 0.1); press(e, 6, 1, 1.1)
    assert e.ran == [] and e.switched == []


def test_combo():
    assert win.parse_combo("Ctrl+Shift+S") == [0x11, 0x10, ord("S")]
    assert win.parse_combo("Win+ArrowLeft") == [0x5B, 0x25]
    assert win.parse_combo("F13") == [0x7C]
    for bad in ("", "Ctrl+Bogus"):
        try:
            win.parse_combo(bad)
        except ValueError:
            continue
        raise AssertionError(bad)


def test_config():
    good = config.validate(copy.deepcopy(config.DEFAULT))
    assert good["ha"] == {"url": config.DEFAULT["ha"]["url"]}
    leaked = copy.deepcopy(config.DEFAULT)
    leaked["ha"]["token"] = "secret"
    assert "token" not in config.validate(leaked)["ha"]
    for broken in ({}, {"profiles": []}, {"profiles": [{"id": "a", "knob": {"type": "volume"}, "keys": []}]}):
        try:
            config.validate(broken)
        except config.ConfigError:
            continue
        raise AssertionError(broken)
    old = copy.deepcopy(config.DEFAULT)
    old["profiles"][0]["led"] = "tidal"
    migrated = config.validate(old)["profiles"][0]
    assert migrated["led"] == "wave" and migrated["live"] == {"type": "none"}
    assert engine._app_names("Photoshop.exe, spotify ; Code") == {"photoshop.exe", "spotify.exe", "code.exe"}


def test_light():
    from keypadstudio.device import light_config
    c = light_config("#ff0000", "steady", 1)
    assert (c.mode, c.brightness, c.color, c.h, c.s, c.v) == (1, 1, 1, 0, 100, 100)
    assert light_config("#4f8cff", "breath", 2).mode == 2
    assert light_config("#4f8cff", "steady", 0).mode == 0  # brightness 0 = off


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"{len(tests)} tests OK")
