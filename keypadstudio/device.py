"""Keypad access through sdcx, serialised on one worker thread.

Each operation opens the device, does its work and closes it again, so a
replugged keypad just works on the next operation.
"""

from __future__ import annotations

import json
import logging
import time
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

from sdcx import Keypad, enumerate_devices
from sdcx.protocol import LightConfig

from .config import BACKUP_PATH

# Key index on the device -> key it should send. Index 16-18 is the knob.
APP_KEYMAP = {0: "f13", 1: "f14", 2: "f15", 3: "f16", 4: "f17", 5: "f18", 16: "f19", 17: "f20", 18: "f21"}
LED_MODES = {"off": 0, "steady": 1, "breath": 2, "press": 3, "tidal": 4}
ORIGINAL_BACKUP = Path(__file__).resolve().parent.parent / "keymap-backup.json"  # saved before this app existed


def light_config(hex_color: str, mode: str, brightness: int) -> LightConfig:
    import colorsys
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    hh, ss, vv = colorsys.rgb_to_hsv(r, g, b)
    m = LED_MODES.get(mode, 1)
    if brightness <= 0:
        m = 0
    return LightConfig(type=1, mode=m, brightness=brightness, speed=2, color=1,
                       h=round(hh * 360) % 360, s=round(ss * 100), v=round(vv * 100))


class Device:
    def __init__(self):
        self._ex = ThreadPoolExecutor(1, thread_name_prefix="keypad")
        self._last_light = None

    def _do(self, fn) -> Future:
        def job():
            with Keypad.open() as pad:
                return fn(pad)
        f = self._ex.submit(job)
        f.add_done_callback(lambda f: f.exception() and logging.warning("keypad: %s", f.exception()))
        return f

    @staticmethod
    def status() -> dict:
        devs = enumerate_devices()
        return {"connected": bool(devs), "model": devs[0].name if devs else "", "usb": devs[0].usb_id if devs else ""}

    def light(self, hex_color: str, mode: str, brightness: int) -> None:
        cfg = light_config(hex_color, mode, brightness)
        if cfg == self._last_light:
            return
        self._last_light = cfg
        # ponytail: set_light persists to the keypad's flash; one write per profile switch is
        # fine for years of use, but don't call this per key press.
        self._do(lambda pad: pad.set_light(cfg))

    def off(self) -> None:
        self._last_light = None
        self._do(lambda pad: pad.off())

    def forget_light(self) -> None:
        self._last_light = None

    def program(self) -> Future:
        def job(pad):
            current = {i: a.name for i, a in pad.get_keymap().items()}
            if not BACKUP_PATH.exists():
                BACKUP_PATH.parent.mkdir(parents=True, exist_ok=True)
                if ORIGINAL_BACKUP.exists():
                    raw = ORIGINAL_BACKUP.read_bytes()  # written by PowerShell, so possibly UTF-16
                    keys = json.loads(raw.decode("utf-16" if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else "utf-8-sig"))["keys"]
                    original = {str(k["key_index"]): k["name"] for k in keys}
                else:
                    original = {str(i): n for i, n in current.items()}
                BACKUP_PATH.write_text(json.dumps(original, indent=2))
            for index, name in APP_KEYMAP.items():
                if current.get(index) != name:
                    pad.set_key(index, name)
                    time.sleep(0.02)  # firmware 106 drops back-to-back writes (sdcx issue #5)
            after = {i: a.name for i, a in pad.get_keymap().items()}
            wrong = [i for i, n in APP_KEYMAP.items() if after.get(i) != n]
            if wrong:
                raise RuntimeError(f"keys {wrong} did not take the new setting; try again")
            return "Keypad programmed. Its keys now only work while Keypad Studio is running."
        return self._do(job)

    def restore(self) -> Future:
        def job(pad):
            if not BACKUP_PATH.exists():
                raise RuntimeError("no backup found; the keypad was never programmed by this app")
            for index, name in json.loads(BACKUP_PATH.read_text()).items():
                pad.set_key(int(index), name)
                time.sleep(0.02)
            return "Original keys restored."
        return self._do(job)
