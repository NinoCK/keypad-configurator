"""Turns F13-F21 events into actions. The hub every other part talks to.

Event indices from the hook: 0-5 keys, 6 knob press, 7 knob right, 8 knob left.
Timing logic (_handle/_fire) takes `now` explicitly so selftest.py can drive it.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from . import actions, config, lighting

HOLD_S = 0.5      # key held this long = hold action
DOUBLE_S = 0.3    # second knob press within this = previous profile
FAST_S, VFAST_S = 0.12, 0.06  # knob tick gaps that count as fast / very fast
POLL_S = 2.0      # how often the live light re-reads its Home Assistant entity
FPS = 20


class Engine:
    def __init__(self, cfg: dict, device=None, osd=None, token_getter=lambda: ""):
        self.cfg = cfg
        self.device, self.osd = device, osd
        self.token_getter = token_getter
        self.on_change = lambda: None  # tray refresh
        self.lock = threading.RLock()
        self.events: queue.Queue = queue.Queue()
        self.timers: dict[str, tuple[float, object]] = {}
        self.down: set[int] = set()
        self.knob_down = self.knob_hold_fired = False
        self.picking: str | None = None
        self.ticks, self.last_tick = 0, 0.0
        self.paused = self.locked = False
        self.auto_from: str | None = None
        self.last_exe = ""
        self.entities: list[str] = []
        self._pool = ThreadPoolExecutor(4, thread_name_prefix="action")
        self._knob_units, self._knob_wake = 0, threading.Event()
        self.live: tuple | None = None  # (profile id, entity, HA state) for the live light
        self.live_error = ""
        self._live_wake = threading.Event()
        self.dark = False

    # ------------------------------------------------------------ config access

    @property
    def settings(self) -> dict:
        return self.cfg["settings"]

    def profile(self, pid: str | None = None) -> dict:
        pid = pid or self.cfg["active"]
        return next((p for p in self.cfg["profiles"] if p["id"] == pid), self.cfg["profiles"][0])

    def ha(self) -> actions.HomeAssistant:
        return actions.HomeAssistant(self.cfg["ha"].get("url", ""), self.token_getter())

    # ------------------------------------------------------------ threads

    def start(self) -> None:
        for target in (self._loop, self._knob_worker, self._auto_switch, self._live_poll, self._render):
            threading.Thread(target=target, daemon=True, name=target.__name__).start()
        self.apply_light()

    def on_key(self, index: int, down: bool) -> None:
        """Called on the hook thread: must return immediately."""
        self.events.put((index, down))

    def _loop(self) -> None:
        while True:
            due = min((t for t, _ in self.timers.values()), default=None)
            timeout = None if due is None else max(0.0, due - time.monotonic())
            try:
                ev = self.events.get(timeout=timeout)
            except queue.Empty:
                ev = None
            now = time.monotonic()
            with self.lock:
                try:
                    self._fire(now)
                    if ev:
                        self._handle(*ev, now)
                except Exception:
                    logging.exception("engine")

    # ------------------------------------------------------------ event logic

    def _fire(self, now: float) -> None:
        for name, (t, cb) in list(self.timers.items()):
            if t <= now and self.timers.get(name, (None,))[0] == t:
                del self.timers[name]
                cb(now)

    def _handle(self, index: int, down: bool, now: float) -> None:
        if down and index in self.down and index < 7:
            return  # auto-repeat while held
        (self.down.add if down else self.down.discard)(index)
        if self.paused:
            return
        if index < 6 and self.settings.get("flip"):
            index = 5 - index  # keypad turned 180 degrees: profiles stay in the order the user sees
        if index < 6:
            self._key(index, down, now)
        elif index == 6:
            self._knob_press(down, now)
        elif down:
            self._turn(1 if index == 7 else -1, now)

    def _key(self, i: int, down: bool, now: float) -> None:
        key = self.profile()["keys"][i]
        has_hold = key["hold"].get("type", "none") != "none"
        name = f"key{i}"
        if not has_hold:
            if down:
                self._run(key["tap"])
            return
        if down:
            self.timers[name] = (now + HOLD_S, lambda _now: self._run(key["hold"]))
        elif name in self.timers:  # released before the hold fired
            del self.timers[name]
            self._run(key["tap"])

    def _knob_press(self, down: bool, now: float) -> None:
        lp = self.settings.get("longPress", "picker")
        if down:
            self.knob_down, self.knob_hold_fired = True, False
            if lp != "none":
                self.timers["knobhold"] = (now + HOLD_S, self._knob_held)
            return
        self.knob_down = False
        self.timers.pop("knobhold", None)
        if self.picking:
            pid, self.picking = self.picking, None
            self.activate(pid)
        elif self.knob_hold_fired:
            pass
        elif self.settings.get("dblPrev"):
            if self.timers.pop("double", None):
                self.cycle(-1)
            else:
                self.timers["double"] = (now + DOUBLE_S, lambda _now: self.cycle(1))
        else:
            self.cycle(1)

    def _knob_held(self, now: float) -> None:
        lp = self.settings.get("longPress", "picker")
        self.knob_hold_fired = True
        if lp == "picker":
            self._pick(0)
        elif lp in ("mute", "mic"):
            self._run({"type": "media", "cmd": lp})

    def _pick(self, delta: int) -> None:
        ids = [p["id"] for p in self.cfg["profiles"]]
        cur = self.picking or self.cfg["active"]
        self.picking = ids[(ids.index(cur) + delta) % len(ids)] if cur in ids else ids[0]
        p = self.profile(self.picking)
        self._osd(f"{p['name']}  (release to choose)", p["color"], sticky=True)

    def _turn(self, direction: int, now: float) -> None:
        if self.knob_down and self.settings.get("longPress", "picker") == "picker":
            self.timers.pop("knobhold", None)
            self.knob_hold_fired = True
            self._pick(direction)
            return
        k = self.profile()["knob"]
        if k.get("invert"):
            direction = -direction
        self.ticks += 1
        if self.ticks % max(1, int(k.get("sens", 1))):
            return
        mult = 1
        if k.get("accel"):
            gap = now - self.last_tick
            mult = 3 if gap < VFAST_S else 2 if gap < FAST_S else 1
        self.last_tick = now
        self._knob(direction * mult)

    # ------------------------------------------------------------ side effects (overridden in selftest)

    def _run(self, action: dict) -> None:
        self._pool.submit(self._run_now, action)

    def _run_now(self, action: dict) -> None:
        try:
            actions.run(action, self.ha, self.activate, self.notify)
        except Exception as e:
            self._fail(e)
        self.refresh_live()

    def _knob(self, units: int) -> None:
        with self.lock:
            self._knob_units += units
        self._knob_wake.set()

    def _knob_worker(self) -> None:
        # Ticks that arrive while a (slow, network) knob action runs are merged into the next one.
        while True:
            self._knob_wake.wait()
            self._knob_wake.clear()
            with self.lock:
                units, self._knob_units = self._knob_units, 0
                knob = dict(self.profile()["knob"])
            try:
                actions.knob(knob, units, self.ha, self.notify)
            except Exception as e:
                self._fail(e)
            self.refresh_live()

    def _fail(self, e: Exception) -> None:
        logging.warning("action failed: %s", e)
        self._osd(f"Failed: {e}", "#ff5a5a")

    def notify(self, text: str, color: str = "#4f8cff", ms: int = 1300) -> None:
        """Feedback for actions with no visible effect (mic, timer, app volume). Always shown."""
        if self.osd:
            self.osd.show(text, color, False, ms)

    def _osd(self, text: str, color: str, sticky: bool = False) -> None:
        if self.osd and (self.settings.get("osd", True) or sticky or color == "#ff5a5a"):
            self.osd.show(text, color, sticky)

    # ------------------------------------------------------------ profiles

    def cycle(self, delta: int) -> None:
        ids = [p["id"] for p in self.cfg["profiles"]]
        cur = self.cfg["active"]
        self.activate(ids[(ids.index(cur) + delta) % len(ids)] if cur in ids else ids[0])

    def activate(self, pid: str, auto: bool = False) -> None:
        with self.lock:
            if not any(p["id"] == pid for p in self.cfg["profiles"]):
                raise actions.ActionError(f"profile {pid!r} no longer exists")
            if not auto:
                self.auto_from = None
            self.cfg["active"] = pid
            self.save()
            p = self.profile()
        self.apply_light()
        self.refresh_live()
        self._osd(p["name"], p["color"])
        self.on_change()

    def apply_light(self) -> None:
        if not self.device:
            return
        b = int(self.settings.get("ledBrightness", 1))
        self.dark = b <= 0 or (self.locked and self.settings.get("sleepLocked"))
        if self.dark:
            self.device.off()
        else:
            self.device.custom(b)

    def set_locked(self, locked: bool) -> None:
        self.locked = locked
        self.apply_light()

    # ------------------------------------------------------------ live light

    def live_entity(self, p: dict | None = None) -> str:
        live = (p or self.profile()).get("live") or {}
        return live.get("entity", "") if live.get("type", "none") != "none" else ""

    def look(self) -> dict:
        p = self.profile()
        live = self.live
        state = live[2] if live and live[:2] == (p["id"], self.live_entity(p)) else None
        return lighting.look(p, state)

    def refresh_live(self) -> None:
        """Re-read the live entity now (after an action), and again shortly after for slow devices."""
        self._live_wake.set()
        t = threading.Timer(1.2, self._live_wake.set)
        t.daemon = True
        t.start()

    def _live_poll(self) -> None:
        while True:
            self._live_wake.wait(POLL_S)
            self._live_wake.clear()
            p = self.profile()
            ent = self.live_entity(p)
            if not ent:
                self.live, self.live_error = None, ""
                continue
            try:
                self.live, self.live_error = (p["id"], ent, self.ha().state(ent)), ""
            except Exception as e:
                self.live_error = str(e)  # keep the last known state; the look falls back if there is none

    def _render(self) -> None:
        cur, start = None, time.monotonic()
        while True:
            time.sleep(1 / FPS)
            if self.dark:
                cur = None
                continue
            lk = self.look()
            target = (*lighting.rgb(lk["color"]), lk["level"])
            # Glide towards the target so brightness and colour changes fade instead of jumping.
            cur = target if cur is None else tuple(c + (t - c) * 0.18 for c, t in zip(cur, target))
            colors = lighting.frame(cur[:3], cur[3], lk["fx"], time.monotonic() - start)
            knob = tuple(round(c * cur[3]) for c in cur[:3])
            self.device.custom(int(self.settings.get("ledBrightness", 1)))  # no-op unless reconnected
            self.device.frame(colors, knob, bool(self.settings.get("flip")))

    def _auto_switch(self) -> None:
        from . import win
        while True:
            time.sleep(0.7)
            if not self.settings.get("autoSwitch") or self.paused:
                continue
            try:
                exe = win.foreground_exe()
            except Exception:
                continue
            if not exe or exe == self.last_exe:
                continue
            self.last_exe = exe
            match = next((p for p in self.cfg["profiles"] if exe in _app_names(p.get("apps", ""))), None)
            if match and match["id"] != self.cfg["active"]:
                if self.auto_from is None:
                    self.auto_from = self.cfg["active"]
                self.activate(match["id"], auto=True)
            elif not match and self.auto_from:
                back, self.auto_from = self.auto_from, None
                if any(p["id"] == back for p in self.cfg["profiles"]):
                    self.activate(back)

    # ------------------------------------------------------------ config

    def save(self) -> None:
        config.save(self.cfg)

    def replace_config(self, new: dict) -> None:
        new = config.validate(new)
        with self.lock:
            new["active"] = self.cfg["active"] if any(p["id"] == self.cfg["active"] for p in new["profiles"]) else new["active"]
            startup_changed = new["settings"]["startup"] != self.settings.get("startup")
            self.cfg = new
            self.save()
        if startup_changed:
            set_startup(new["settings"]["startup"])
        self.apply_light()
        self.refresh_live()
        self.on_change()


def _app_names(text: str) -> set[str]:
    names = set()
    for part in text.replace(";", ",").split(","):
        n = part.strip().lower()
        if n:
            names.add(n if n.endswith(".exe") else n + ".exe")
    return names


def set_startup(on: bool) -> None:
    """Add/remove the HKCU Run entry that starts the app in the tray at sign-in."""
    import sys
    import winreg
    from pathlib import Path
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    launcher = Path(__file__).resolve().parent.parent / "keypad-studio.pyw"
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_SET_VALUE) as k:
        if on:
            winreg.SetValueEx(k, "KeypadStudio", 0, winreg.REG_SZ, f'"{pythonw}" "{launcher}" --background')
        else:
            try:
                winreg.DeleteValue(k, "KeypadStudio")
            except FileNotFoundError:
                pass
