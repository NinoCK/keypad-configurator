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

    def activate(self, pid, auto=False, quiet=False):
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
    k.update(accel=False)
    e.knobbed.clear()
    for t, i in ((20.0, 8), (20.1, 8), (20.15, 7), (20.3, 8), (21.0, 7), (21.1, 7)):
        e._handle(i, True, t)
    assert e.knobbed == [-1, -1, -1, 1, 1], e.knobbed  # stray right click at 20.15 dropped; real reversal at 21.0 kept


def test_brightness_knob():
    from keypadstudio import actions
    sent = []

    class StaleHA:  # HA still reports the light at 50% however often we change it
        def state(self, e): return {"state": "on", "attributes": {"brightness": 128}}
        def call(self, d, s, data): sent.append((s, data.get("brightness_pct")))

    k = {"type": "ha_brightness", "entity": "light.x", "step": 25}
    actions._level.clear()
    for units in (-2, -1, 3, 9):
        actions.knob(k, units, StaleHA)
    assert sent == [("turn_off", None), ("turn_off", None), ("turn_on", 75), ("turn_on", 100)], sent  # never back on from stale state


def test_keys_mode():
    e = FakeEngine(mode="keys", longPress="none")
    e.profile("pc")["knob"]["press"] = {"type": "media", "label": "press"}
    press(e, 1, 0, 0.1)
    assert e.switched == ["pc"] and e.ran == [], (e.switched, e.ran)
    press(e, 4, 1, 1.1)                  # no 5th profile: nothing
    press(e, 6, 2, 2.1)
    assert e.switched == ["pc"] and e.ran == ["press"], (e.switched, e.ran)


def test_ha_actions():
    from keypadstudio import actions
    sent = []

    class HA:
        st = {}
        def state(self, e): return self.st
        def call(self, d, s, data): sent.append((s, data.get("percentage") or data.get("hvac_mode") or data.get("rgb_color")))

    fan = {"type": "ha_fan_speed", "entity": "fan.x", "step": 34}  # the purifier template's strength
    HA.st = {"state": "on", "attributes": {"percentage": 33, "percentage_step": 33.33}}
    actions.knob(fan, 1, HA)             # level 1 -> 2 must be 66, not 67 (purifiers round 67 up to level 3)
    HA.st["attributes"]["percentage"] = 66
    actions.knob(fan, 1, HA)             # 2 -> 3
    HA.st["attributes"]["percentage"] = 100
    actions.knob(fan, 1, HA)             # already top: nothing
    HA.st["attributes"]["percentage"] = 33
    actions.knob(fan, -1, HA)            # already lowest: nothing, never off
    HA.st = {"state": "off", "attributes": {"percentage_step": 33.33}}
    actions.knob(fan, -3, HA)            # off stays off
    actions.knob(fan, 1, HA)             # off -> level 1
    assert sent == [("set_percentage", 66), ("set_percentage", 100), ("set_percentage", 33)], sent
    sent.clear()
    HA.st = {"state": "heat", "attributes": {"hvac_modes": ["off", "cool", "heat", "auto"]}}
    actions.run({"type": "ha_climate_cycle", "entity": "climate.x"}, HA, None)
    HA.st["state"] = "auto"
    actions.run({"type": "ha_climate_cycle", "entity": "climate.x"}, HA, None)  # wraps, skips off
    cyc = {"type": "ha_color_cycle", "entity": "light.x", "count": 2, "colors": ["#ff0000", "#00ff00", "#0000ff"]}
    actions._cycle.clear()
    for _ in range(3):
        actions.run(cyc, HA, None)
    assert sent == [("set_hvac_mode", "auto"), ("set_hvac_mode", "cool"),
                    ("turn_on", [255, 0, 0]), ("turn_on", [0, 255, 0]), ("turn_on", [255, 0, 0])], sent


def test_ac_settle():
    import threading
    import time
    from keypadstudio import actions
    sent = []

    class HA:
        def state(self, e): return {"attributes": {"temperature": 24}}
        def call(self, d, s, data): sent.append(data["temperature"])

    e = engine.Engine(copy.deepcopy(config.DEFAULT))
    e.profile()["knob"].update(type="ha_climate_temp", entity="climate.x", step=1)
    e.ha, e.notify, e.refresh_live = HA, lambda *a, **k: None, lambda: None
    old, engine.SETTLE_S = engine.SETTLE_S, 0.15
    try:
        threading.Thread(target=e._knob_worker, daemon=True).start()
        e._knob(1); time.sleep(0.05); e._knob(-1); time.sleep(0.3)   # nudge and back: nothing sent
        e._knob(1); time.sleep(0.05); e._knob(1); time.sleep(0.05)
        assert sent == [], sent                                        # still turning
        time.sleep(0.3)
        assert sent == [25.0], sent                                    # one request after it settles
    finally:
        engine.SETTLE_S = old


def test_flip():
    e = FakeEngine(flip=True)
    e.profile()["keys"][0]["tap"]["label"] = "top-left"
    press(e, 5, 0, 0.1)  # physical key 6 is top-left when the pad is turned around
    assert e.ran == ["top-left"], e.ran


def test_lighting():
    from keypadstudio.lighting import DEFAULT_AIR, DEFAULT_CLIMATE, EFFECTS, FLOOR, firmware, look, migrate
    p = {"color": "#102030", "led": "steady"}
    assert look(p, None)["color"] == "#102030"
    ac = {**p, "live": {"type": "climate", "entity": "climate.x"}}
    assert look(ac, {"state": "cool", "attributes": {"temperature": 24}})["fx"] == "wave"
    assert look(ac, {"state": "off", "attributes": {}})["color"] == "#102030"
    assert look(ac, {"state": "unavailable"})["fx"] == "dim"
    air = {**p, "live": {"type": "air"}}
    assert [look(air, {"state": v})["color"] for v in ("5", "20", "40", "300")] == [b["color"] for b in DEFAULT_AIR]
    lamp = {**p, "live": {"type": "light", "useColor": False}}
    assert look(lamp, {"state": "on", "attributes": {"brightness": 128}})["level"] == 0.5
    assert look(lamp, {"state": "off"})["level"] == FLOOR
    assert firmware({"color": "#fff", "fx": "dim", "level": 1})[3] < 0.2
    # every AC mode looks different: colour, firmware effect or speed
    looks = [(r["color"], EFFECTS[r["fx"]]) for r in DEFAULT_CLIMATE.values()]
    assert len(set(looks)) == len(looks)
    old = {"led": "tidal", "live": {"type": "climate", "modes": {"heat": {"color": "#f00", "fx": "flicker"}}}}
    migrate(old)
    assert old["led"] == "wave" and old["live"]["modes"] == DEFAULT_CLIMATE


def test_device_light():
    import time
    from keypadstudio import device
    writes = []

    class Pad:
        def set_light(self, c): writes.append(c)
        def close(self): pass

    real, device.Keypad = device.Keypad, type("K", (), {"open": staticmethod(Pad)})
    gap, device.MIN_WRITE_GAP = device.MIN_WRITE_GAP, 0.2
    try:
        d = device.Device()
        a, b, c = (device.light_config(x, "steady", 1) for x in ("#ff0000", "#00ff00", "#0000ff"))
        d.light(a); time.sleep(0.05)
        d.light(b); d.light(c); time.sleep(0.35)   # inside the gap: only the latest is written
        d.light(c); time.sleep(0.05)               # unchanged: nothing written
        assert writes == [a, c], writes
    finally:
        device.Keypad, device.MIN_WRITE_GAP = real, gap


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
    assert light_config("#ff0000", "steady", 1, level=0.5).v == 50


def test_home_timeout():
    e = FakeEngine(homeProfile="desk", homeAfter=10)
    e.cfg["active"] = "pc"
    e._arm_home(0)
    e._fire(5); e._handle(8, True, 5)  # activity restarts the countdown
    e._fire(14); assert e.switched == []
    e._handle(0, True, 20)  # still held when the deadline passes: wait
    e._fire(30); assert e.switched == []
    e._handle(0, False, 31)
    e._fire(41); assert e.switched == ["desk"], e.switched
    e.settings["homeAfter"] = 0
    e._arm_home(50); assert "home" not in e.timers


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"{len(tests)} tests OK")
