"""What a key press or knob turn does. Pure dispatch; the only state is the knob's last brightness."""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import urllib.error
import urllib.request

from . import win

# entity -> (brightness % we last sent, when). While the knob turns, HA's state lags behind the
# light (fades, slow reports), so relative steps computed by HA overshoot or switch it back on.
_level: dict[str, tuple[int, float]] = {}
LEVEL_TRUST_S = 3.0  # after this long idle, re-read the real brightness (someone may have changed it)
_cycle: dict[str, int] = {}  # entity -> index of the colour ha_color_cycle last set


class ActionError(Exception):
    pass


class HomeAssistant:
    def __init__(self, url: str, token: str):
        if not url or not token:
            raise ActionError("Home Assistant is not set up (address and token on the Home Assistant page)")
        self.url, self.token = url.rstrip("/"), token

    def _req(self, path: str, body: object = None, timeout: float = 5):
        req = urllib.request.Request(
            self.url + path, method="GET" if body is None else "POST",
            data=None if body is None else json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = r.read()
                return json.loads(data) if data else None
        except urllib.error.HTTPError as e:
            raise ActionError({401: "Home Assistant rejected the token", 404: f"not found: {path}",
                               500: "the device refused (details in the Home Assistant log)"}.get(e.code, f"Home Assistant error {e.code}")) from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise ActionError(f"cannot reach Home Assistant at {self.url}") from e

    def call(self, domain: str, service: str, data: dict) -> None:
        self._req(f"/api/services/{domain}/{service}", data)

    def state(self, entity: str) -> dict:
        return self._req(f"/api/states/{entity}")

    def ping(self) -> str:
        return (self._req("/api/") or {}).get("message", "ok")

    def entities(self) -> list[dict]:
        """Every entity with what the settings window needs to offer it: name, modes, sensor class."""
        out = []
        for s in self._req("/api/states", timeout=10) or []:
            at = s.get("attributes") or {}
            out.append({"id": s["entity_id"], "name": at.get("friendly_name", ""),
                        "opts": at.get("hvac_modes" if s["entity_id"].startswith("climate.") else "preset_modes") or [],
                        "dc": at.get("device_class", ""), "step": at.get("percentage_step")})
        return sorted(out, key=lambda e: e["id"])


def _rgb(hex_color: str) -> list[int]:
    h = (hex_color or "#ffffff").lstrip("#")
    return [int(h[i:i + 2], 16) for i in (0, 2, 4)]


def _name(state: dict, fallback: str) -> str:
    return (state.get("attributes") or {}).get("friendly_name") or fallback


def _need(a: dict, k: str) -> str:
    v = str(a.get(k) or "").strip()
    if not v:
        raise ActionError(f"'{k}' is empty")
    return v


_focus_timer: threading.Timer | None = None


def _focus(minutes: int, notify) -> None:
    global _focus_timer
    if _focus_timer and _focus_timer.is_alive():
        _focus_timer.cancel()
        _focus_timer = None
        notify("Focus timer cancelled")
        return

    def done():
        import winsound
        winsound.MessageBeep(winsound.MB_ICONASTERISK)
        notify("Focus time is up", "#3ad29f", 8000)

    _focus_timer = threading.Timer(minutes * 60, done)
    _focus_timer.daemon = True
    _focus_timer.start()
    notify(f"Focus timer: {minutes} min")


def _no_notify(text, color="#4f8cff", ms=1300):
    pass


def run(a: dict, ha, switch_profile, notify=_no_notify) -> None:
    """Run one key action. `ha` is a zero-arg callable returning a HomeAssistant.

    `notify(text, color, ms)` shows feedback on screen for actions with no visible effect.
    """
    t = a.get("type", "none")
    if t == "none":
        return
    if t.startswith("ha_"):
        if t == "ha_service":
            domain, _, service = _need(a, "service").partition(".")
            if not service:
                raise ActionError("service must look like domain.service, e.g. light.turn_on")
            try:
                data = json.loads(a.get("data") or "{}")
            except ValueError as e:
                raise ActionError("extra data is not valid JSON") from e
            if a.get("entity"):
                data.setdefault("entity_id", a["entity"])
            ha().call(domain, service, data)
            return
        e = {"entity_id": _need(a, "entity")}
        if t == "ha_light":
            ha().call("light", "turn_on", {**e, "rgb_color": _rgb(a.get("color")), "brightness_pct": int(a.get("brightness", 100))})
        elif t == "ha_white":
            ha().call("light", "turn_on", {**e, "color_temp_kelvin": int(a.get("kelvin", 4000)), "brightness_pct": int(a.get("brightness", 100))})
        elif t == "ha_toggle":
            ha().call("homeassistant", "toggle", e)
        elif t == "ha_scene":
            ha().call("scene", "turn_on", e)
        elif t == "ha_automation":
            ha().call("automation", "trigger", e)
        elif t == "ha_script":
            ha().call("script", "turn_on", e)
        elif t == "ha_fan_preset":
            ha().call("fan", "turn_on", {**e, "preset_mode": _need(a, "preset")})  # also works while it is off
        elif t == "ha_climate_mode":
            ha().call("climate", "set_hvac_mode", {**e, "hvac_mode": _need(a, "mode")})
        elif t == "ha_color_cycle":
            colors = [c for c in (a.get("colors") or [])[:max(1, min(8, int(a.get("count", 8))))] if c]
            if not colors:
                raise ActionError("no colours set")
            i = _cycle[e["entity_id"]] = (_cycle.get(e["entity_id"], -1) + 1) % len(colors)
            ha().call("light", "turn_on", {**e, "rgb_color": _rgb(colors[i])})
        elif t == "ha_climate_cycle":
            st = ha().state(e["entity_id"]) or {}
            modes = [m for m in (st.get("attributes") or {}).get("hvac_modes") or [] if m != "off"]
            if not modes:
                raise ActionError(f"{e['entity_id']} reports no modes")
            cur = st.get("state")
            new = modes[(modes.index(cur) + 1) % len(modes)] if cur in modes else modes[0]
            ha().call("climate", "set_hvac_mode", {**e, "hvac_mode": new})
            notify(f"{_name(st, e['entity_id'])}: {new.replace('_', ' ')}")
        else:
            raise ActionError(f"unknown action {t}")
    elif t == "launch":
        path = os.path.expandvars(_need(a, "path"))
        args = a.get("args") or ""
        os.startfile(path, arguments=args) if args else os.startfile(path)
    elif t == "url":
        url = _need(a, "url")
        if not url.lower().startswith(("http://", "https://")):
            raise ActionError("website address must start with http:// or https://")
        os.startfile(url)
    elif t == "hotkey":
        win.send_combo(win.parse_combo(_need(a, "keys")))
    elif t == "text":
        win.type_text(a.get("text") or "")
    elif t == "media" and a.get("cmd") == "mic":
        from . import audio
        notify("Microphone muted" if audio.toggle_mic() else "Microphone on", "#ff9f43")
    elif t == "media":
        win.tap(win.VK_MEDIA[a.get("cmd", "play_pause")])
    elif t == "audio_out":
        from . import audio
        try:
            notify(f"Sound: {audio.cycle_output(_need(a, 'device').splitlines())}")
        except LookupError as e:
            raise ActionError(str(e)) from e
    elif t == "timer":
        _focus(int(a.get("minutes", 25)), notify)
    elif t == "command":
        flags = 0x08000000 if a.get("hidden", True) else 0x00000010  # CREATE_NO_WINDOW / CREATE_NEW_CONSOLE
        subprocess.Popen(["powershell", "-NoProfile", "-Command", _need(a, "cmd")], creationflags=flags)
    elif t == "profile":
        switch_profile(_need(a, "profile"))
    else:
        raise ActionError(f"unknown action {t}")


def knob(k: dict, units: int, ha, notify=_no_notify) -> None:
    """Apply `units` knob steps (negative = left). One unit = the profile's strength."""
    if not units:
        return
    t, step, sign = k.get("type", "volume"), max(1, int(k.get("step", 2))), 1 if units > 0 else -1
    amount = abs(units) * step
    if t == "volume":
        win.tap(win.VK_VOLUME_UP if sign > 0 else win.VK_VOLUME_DOWN, max(1, round(amount / 2)))  # Windows moves 2% per key
    elif t == "app_volume":
        from . import audio
        app = _need(k, "app")
        level = audio.app_volume(app, sign * amount / 100)
        if level is None:
            raise ActionError(f"{app} is not playing audio right now")
        notify(f"{app.removesuffix('.exe').capitalize()}: {round(level * 100)}%")
    elif t == "ha_brightness":
        e = _need(k, "entity")
        cur, when = _level.get(e, (0, -LEVEL_TRUST_S))
        if time.monotonic() - when > LEVEL_TRUST_S:
            st = ha().state(e) or {}
            b = (st.get("attributes") or {}).get("brightness")
            cur = (100 if b is None else round(b / 2.55)) if st.get("state") == "on" else 0
        new = max(0, min(100, cur + sign * amount))
        if new:
            ha().call("light", "turn_on", {"entity_id": e, "brightness_pct": new})
        else:
            ha().call("light", "turn_off", {"entity_id": e})
        _level[e] = (new, time.monotonic())
    elif t == "ha_colortemp":
        e = _need(k, "entity")
        at = (ha().state(e) or {}).get("attributes", {})
        lo, hi = at.get("min_color_temp_kelvin") or 2000, at.get("max_color_temp_kelvin") or 6500
        cur = at.get("color_temp_kelvin") or (lo + hi) // 2
        new = min(hi, max(lo, cur - sign * amount * (hi - lo) / 100))  # right = warmer = lower kelvin
        ha().call("light", "turn_on", {"entity_id": e, "color_temp_kelvin": int(new)})
    elif t == "ha_media_volume":
        e = _need(k, "entity")
        cur = ((ha().state(e) or {}).get("attributes", {}).get("volume_level")) or 0
        ha().call("media_player", "volume_set", {"entity_id": e, "volume_level": round(min(1, max(0, cur + sign * amount / 100)), 2)})
    elif t == "ha_fan_speed":
        # Left stops at the lowest speed, never off (on/off is a key or the knob press); right tops out at 100%.
        e = _need(k, "entity")
        st = ha().state(e) or {}
        at = st.get("attributes") or {}
        lo, pct = max(1, int(at.get("percentage_step") or 1)), at.get("percentage")
        if sign > 0:
            ha().call("fan", "increase_speed", {"entity_id": e, "percentage_step": min(amount, 100)})
        elif st.get("state") != "on" or (pct is not None and pct <= lo):
            pass
        elif pct is None or pct - amount < lo:
            ha().call("fan", "set_percentage", {"entity_id": e, "percentage": lo})
        else:
            ha().call("fan", "decrease_speed", {"entity_id": e, "percentage_step": min(amount, 100)})
        st = ha().state(e) or {}
        pct = (st.get("attributes") or {}).get("percentage")
        notify(f"{_name(st, e)}: {'off' if st.get('state') == 'off' else f'{pct}%'}")
    elif t == "ha_climate_temp":
        e = _need(k, "entity")
        st = ha().state(e) or {}
        new = climate_target(st, units, k)
        ha().call("climate", "set_temperature", {"entity_id": e, "temperature": new})
        notify(f"{_name(st, e)}: {new:g}°")
    elif t == "scroll":
        win.wheel(sign * 40 * amount)  # 120 per notch = 3 lines, so 40 per line
    elif t == "zoom":
        win.wheel(sign * 120 * amount, ctrl=True)
    elif t == "keys":
        combo = win.parse_combo(_need(k, "cw" if sign > 0 else "ccw"))
        for _ in range(amount):
            win.send_combo(combo)
    else:
        raise ActionError(f"unknown knob action {t}")


def climate_target(st: dict, units: int, k: dict) -> float:
    """Temperature `units` knob steps from the AC's current setting. One step = 0.5 degree x strength."""
    at = st.get("attributes") or {}
    lo, hi = at.get("min_temp", 16), at.get("max_temp", 30)
    return min(hi, max(lo, (at.get("temperature") or 24) + units * max(1, int(k.get("step", 1))) * 0.5))
