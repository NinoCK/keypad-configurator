"""What the keypad LEDs show: a profile's look (colour, effect, level) and the frames that draw it.

Frames are drawn on the host and streamed as per-key colours (light mode 5), so
nothing here writes the keypad's flash. Positions 0-5 are the keys as the user
sees them (top row 0-2, bottom row 3-5); device.py maps them to key indices,
which is where the 180 degree rotation is applied.
"""

from __future__ import annotations

import math

EFFECTS = ("steady", "breathe", "wave", "spin", "flicker", "drops", "blink", "dim", "off")
OLD_EFFECTS = {"breath": "breathe", "tidal": "wave", "press": "steady"}  # firmware-era names

MODE_NAMES = {"cool": "Cooling", "heat": "Heating", "auto": "Auto", "heat_cool": "Heat/cool",
              "dry": "Drying", "fan_only": "Fan only", "off": "Off"}
DEFAULT_CLIMATE = {
    "cool": {"color": "#2f8bff", "fx": "wave"},
    "heat": {"color": "#ff5a1f", "fx": "flicker"},
    "auto": {"color": "#3ad29f", "fx": "breathe"},
    "heat_cool": {"color": "#b06bff", "fx": "breathe"},
    "dry": {"color": "#f5c542", "fx": "drops"},
    "fan_only": {"color": "#dfe8ff", "fx": "spin"},
    "off": {"color": "", "fx": "dim"},  # "" = the profile's own colour
}
# PM2.5 in µg/m³, US EPA breakpoints. `max: None` = everything above.
DEFAULT_AIR = [
    {"name": "Good", "max": 12, "color": "#3ad29f", "fx": "steady"},
    {"name": "Moderate", "max": 35, "color": "#f5d442", "fx": "steady"},
    {"name": "Poor", "max": 55, "color": "#ff9f1a", "fx": "breathe"},
    {"name": "Unhealthy", "max": None, "color": "#ff3b30", "fx": "blink"},
]

RING = (0, 1, 2, 5, 4, 3)  # clockwise around the six keys
FLOOR = 0.06               # level for "on but nothing to show" (light off), so the pad never looks dead


def rgb(hex_color: str) -> tuple[int, int, int]:
    h = (hex_color or "#ffffff").lstrip("#")
    try:
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return (255, 255, 255)


def look(profile: dict, state: dict | None) -> dict:
    """The look for a profile given its live entity's HA state (None = unknown/no live source)."""
    color, fx = profile.get("color", "#ffffff"), profile.get("led", "steady")
    base = {"color": color, "fx": fx, "level": 1.0, "caption": ""}
    live = profile.get("live") or {}
    kind = live.get("type", "none")
    if kind == "none" or not state:
        return base
    s, at = str(state.get("state", "")), state.get("attributes") or {}
    if s in ("unavailable", "unknown"):
        return {**base, "fx": "dim", "caption": "Unavailable"}
    if kind == "light":
        if s != "on":
            return {"color": color, "fx": "steady", "level": FLOOR, "caption": "Off"}
        b = (at.get("brightness") or 255) / 255
        c = at.get("rgb_color")
        if live.get("useColor", True) and c:
            color = "#%02x%02x%02x" % tuple(c)
        return {"color": color, "fx": fx, "level": max(FLOOR, b), "caption": f"On · {round(b * 100)}%"}
    if kind == "climate":
        row = {**DEFAULT_CLIMATE, **(live.get("modes") or {})}.get(s)
        if not row:
            return base
        cap = MODE_NAMES.get(s, s)
        if s != "off" and at.get("temperature") is not None:
            cap += f" · set {at['temperature']:g}°"
        if at.get("current_temperature") is not None:
            cap += f" · room {at['current_temperature']:g}°"
        return {"color": row.get("color") or color, "fx": row.get("fx", "steady"), "level": 1.0, "caption": cap}
    if kind == "air":
        try:
            v = float(s)
        except ValueError:
            return base
        bands = live.get("bands") or DEFAULT_AIR
        row = next((b for b in bands if b.get("max") in (None, "") or v <= float(b["max"])), bands[-1])
        unit = at.get("unit_of_measurement", "")
        return {"color": row["color"], "fx": row["fx"], "level": 1.0, "caption": f"{row.get('name', '')} · {v:g} {unit}".strip(" ·")}
    return base


def levels(fx: str, t: float) -> list[float]:
    """Brightness 0..1 of each key position at time t (seconds). Keep in sync with ui.html."""
    if fx == "off":
        return [0.0] * 6
    if fx == "dim":
        return [0.15] * 6
    if fx == "breathe":  # slow sine, about 3 s per breath
        return [0.12 + 0.88 * (0.5 - 0.5 * math.cos(t * 2.1))] * 6
    if fx == "wave":     # left to right, like cold air rolling across
        return [0.12 + 0.88 * (0.5 + 0.5 * math.sin(t * 3.0 - (p % 3) * 1.4)) for p in range(6)]
    if fx == "spin":     # one bright key running clockwise with a fading tail, like a fan
        head, out = (t * 5.0) % 6, [0.0] * 6
        for i, p in enumerate(RING):
            out[p] = max(0.08, 1 - ((head - i) % 6) * 0.38)
        return out
    if fx == "flicker":  # uneven glow, like embers
        return [min(1, max(0.25, 0.62 + 0.22 * math.sin(t * 5.3 + p * 1.9) + 0.18 * math.sin(t * 9.1 + p * 0.7))) for p in range(6)]
    if fx == "drops":    # keys light and fade at their own pace, like drips
        return [0.08 + 0.92 * (1 - ((t * (0.45 + 0.11 * p) + p * 0.618) % 1)) ** 4 for p in range(6)]
    if fx == "blink":
        return [1.0 if t % 1.2 < 0.6 else 0.1] * 6
    return [1.0] * 6


def frame(color: tuple[float, float, float], level: float, fx: str, t: float) -> list[tuple[int, int, int]]:
    return [tuple(round(max(0, min(255, c * level * k))) for c in color) for k in levels(fx, t)]

