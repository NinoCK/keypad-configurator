"""What the keypad LEDs show: a profile's look (colour, effect, level).

Effects are the keypad firmware's own, so the keypad animates them itself and the
app only writes when the look changes (see device.py for why).
"""

from __future__ import annotations

# ponytail: the vendor docs don't say which way speed runs; assumed higher = faster. Swap these if not.
SLOW, FAST = 1, 4
# effect -> (firmware mode, speed)
EFFECTS = {
    "steady": ("steady", 0), "breathe": ("breath", SLOW), "pulse": ("breath", FAST),
    "wave": ("tidal", SLOW), "rush": ("tidal", FAST), "press": ("press", 2),
    "dim": ("steady", 0), "off": ("off", 0),
    # Firmware palette mode (light config colour=0): the keypad cycles colours itself.
    "rainbow": ("steady", 0), "rainbow-breathe": ("breath", SLOW), "rainbow-wave": ("tidal", SLOW),
}
DIM = 0.15
# Names used by earlier versions (firmware-era, then host-drawn).
OLD_EFFECTS = {"breath": "breathe", "tidal": "wave", "spin": "rush", "flicker": "breathe", "drops": "pulse", "blink": "pulse"}

MODE_NAMES = {"cool": "Cooling", "heat": "Heating", "auto": "Auto", "heat_cool": "Heat/cool",
              "dry": "Drying", "fan_only": "Fan only", "off": "Off"}
DEFAULT_CLIMATE = {
    "cool": {"color": "#2f8bff", "fx": "wave"},
    "heat": {"color": "#ff5a1f", "fx": "breathe"},
    "auto": {"color": "#3ad29f", "fx": "steady"},
    "heat_cool": {"color": "#b06bff", "fx": "steady"},
    "dry": {"color": "#f5c542", "fx": "pulse"},
    "fan_only": {"color": "#dfe8ff", "fx": "rush"},
    "off": {"color": "", "fx": "dim"},  # "" = the profile's own colour
}
# PM2.5 in µg/m³, US EPA breakpoints. `max: None` = everything above.
DEFAULT_AIR = [
    {"name": "Good", "max": 12, "color": "#3ad29f", "fx": "steady"},
    {"name": "Moderate", "max": 35, "color": "#f5d442", "fx": "steady"},
    {"name": "Poor", "max": 55, "color": "#ff9f1a", "fx": "breathe"},
    {"name": "Unhealthy", "max": None, "color": "#ff3b30", "fx": "pulse"},
]

FLOOR = 0.06  # level for "on but nothing to show" (light off), so the pad never looks dead


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
        b = round((at.get("brightness") or 255) / 255 * 20) / 20  # 5% steps: fewer keypad writes
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


def migrate(profile: dict) -> None:
    """Bring effect names saved by older versions up to date (in place)."""
    fix = lambda fx: (lambda f: f if f in EFFECTS else "steady")(OLD_EFFECTS.get(fx, fx))
    profile["led"] = fix(profile.get("led"))
    live = profile.get("live") or {}
    rows = list((live.get("modes") or {}).values()) + list(live.get("bands") or [])
    if any(r.get("fx") not in EFFECTS for r in rows):
        # Saved from the host-drawn defaults, which don't map one to one: take the new defaults.
        if "modes" in live:
            live["modes"] = {m: dict(r) for m, r in DEFAULT_CLIMATE.items()}
        if "bands" in live:
            live["bands"] = [{**b, "fx": fix(b.get("fx"))} for b in live["bands"]]


def firmware(lk: dict) -> tuple[str, str, int, float]:
    """(colour, firmware mode, speed, level) for device.light_config."""
    mode, speed = EFFECTS.get(lk["fx"], EFFECTS["steady"])
    color = "rainbow" if lk["fx"].startswith("rainbow") else lk["color"]
    return color, mode, speed, lk["level"] * (DIM if lk["fx"] == "dim" else 1)
