"""Config file (%USERPROFILE%\\.keypadstudio\\config.json): defaults, validation, atomic save."""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path

# Not %APPDATA%: when a packaged app (e.g. Claude) starts us, Windows silently redirects AppData
# writes into that package's private copy, so settings saved there vanish when started normally.
APP_DIR = Path.home() / ".keypadstudio"
CONFIG_PATH = APP_DIR / "config.json"
BACKUP_PATH = APP_DIR / "keymap-backup.json"
INSTANCE_PATH = APP_DIR / "instance.json"
LOG_PATH = APP_DIR / "log.txt"
TOKEN_TARGET = "KeypadStudio/HomeAssistant"


def _key(label, tap):
    return {"label": label, "tap": tap, "hold": {"type": "none"}}


DEFAULT = {
    "active": "desk",
    "profiles": [
        {"id": "desk", "name": "Desk light", "color": "#ffb347", "led": "steady", "apps": "",
         "knob": {"type": "ha_brightness", "entity": "light.desk_lamp", "step": 10, "sens": 1, "accel": False, "invert": False},
         "keys": [
             _key("Warm", {"type": "ha_white", "entity": "light.desk_lamp", "kelvin": 2700, "brightness": 80}),
             _key("Daylight", {"type": "ha_white", "entity": "light.desk_lamp", "kelvin": 5000, "brightness": 100}),
             _key("Red", {"type": "ha_light", "entity": "light.desk_lamp", "color": "#ff3b30", "brightness": 60}),
             _key("On / off", {"type": "ha_toggle", "entity": "light.desk_lamp"}),
             _key("Movie", {"type": "ha_scene", "entity": "scene.movie_night"}),
             _key("Good night", {"type": "ha_automation", "entity": "automation.good_night"}),
         ]},
        {"id": "pc", "name": "PC", "color": "#4f8cff", "led": "steady", "apps": "",
         "knob": {"type": "volume", "step": 2, "sens": 1, "accel": True, "invert": False},
         "keys": [
             _key("Browser", {"type": "url", "url": "https://www.google.com"}),
             _key("Notepad", {"type": "launch", "path": "notepad.exe", "args": ""}),
             _key("Terminal", {"type": "launch", "path": "wt.exe", "args": ""}),
             _key("Play/pause", {"type": "media", "cmd": "play_pause"}),
             _key("Next", {"type": "media", "cmd": "next"}),
             _key("Snip", {"type": "hotkey", "keys": "Win+Shift+S"}),
         ]},
    ],
    "ha": {"url": "http://homeassistant.local:8123"},
    "settings": {"startup": False, "osd": True, "dblPrev": False, "longPress": "picker",
                 "autoSwitch": False, "sleepLocked": True, "ledBrightness": 1, "flip": False,
                 "mode": "knob"},  # knob: press = next profile. keys: keys 1-6 = profiles, press = knob.press
}


class ConfigError(ValueError):
    pass


def validate(cfg: object) -> dict:
    """Shape check for anything written by the UI. Returns a cleaned copy."""
    def need(cond, msg):
        if not cond:
            raise ConfigError(msg)

    need(isinstance(cfg, dict), "config must be an object")
    profiles = cfg.get("profiles")
    need(isinstance(profiles, list) and profiles, "at least one profile is required")
    ids = set()
    for p in profiles:
        need(isinstance(p, dict) and isinstance(p.get("id"), str) and p["id"], "every profile needs an id")
        need(p["id"] not in ids, f"duplicate profile id {p['id']}")
        ids.add(p["id"])
        need(isinstance(p.get("knob"), dict) and isinstance(p["knob"].get("type"), str), "every profile needs a knob")
        keys = p.get("keys")
        need(isinstance(keys, list) and len(keys) == 6, "every profile needs exactly 6 keys")
        for k in keys:
            for slot in ("tap", "hold"):
                need(isinstance(k.get(slot), dict) and isinstance(k[slot].get("type"), str), "every key needs a press and hold action")
    out = copy.deepcopy(cfg)
    from .lighting import migrate
    for p in out["profiles"]:
        migrate(p)
        if not isinstance(p["knob"].get("press"), dict) or not isinstance(p["knob"]["press"].get("type"), str):
            p["knob"]["press"] = {"type": "none"}
        if not isinstance(p.get("live"), dict) or p["live"].get("type") not in ("none", "light", "climate", "air"):
            p["live"] = {"type": "none"}
    out["ha"] = {"url": str((cfg.get("ha") or {}).get("url", ""))}  # the token never goes in this file
    out["settings"] = {**DEFAULT["settings"], **(cfg.get("settings") or {})}
    if out.get("active") not in ids:
        out["active"] = profiles[0]["id"]
    out.pop("ideas", None)
    return out


def load() -> dict:
    try:
        return validate(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return copy.deepcopy(DEFAULT)
    except (ValueError, OSError) as exc:
        # Keep the broken file for the user instead of silently overwriting it.
        bad = CONFIG_PATH.with_suffix(".broken.json")
        CONFIG_PATH.replace(bad)
        import logging
        logging.error("config.json was unreadable (%s); moved to %s and started from defaults", exc, bad)
        return copy.deepcopy(DEFAULT)


def save(cfg: dict) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    os.replace(tmp, CONFIG_PATH)
