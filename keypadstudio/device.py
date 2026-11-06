"""Keypad access through sdcx, serialised on one worker thread.

The pad stays open between operations (frames arrive 20 times a second); any
failure closes it, so a replugged keypad is reopened on the next operation.
"""

from __future__ import annotations

import json
import logging
import time
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

from sdcx import Keypad, enumerate_devices
from sdcx.protocol import CMD_SET_KEY_RGB_BULK, LightConfig, u16le

from .config import BACKUP_PATH

# Key index on the device -> key it should send. Index 16-18 is the knob.
APP_KEYMAP = {0: "f13", 1: "f14", 2: "f15", 3: "f16", 4: "f17", 5: "f18", 16: "f19", 17: "f20", 18: "f21"}
LED_MODES = {"off": 0, "steady": 1, "breath": 2, "press": 3, "tidal": 4, "custom": 5}
KNOB_LEDS = (16, 17, 18)
WRITE_GAP = 0.011  # firmware 106 drops writes closer together than ~10 ms (measured; sdcx issue #5)
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
        self._pad = None
        self._last_light = None
        self._table, self._sent_at = b"", 0.0
        self._frame_busy, self._retry_at = False, 0.0

    def _close(self) -> None:
        if self._pad:
            try:
                self._pad.close()
            except Exception:
                pass
        self._pad, self._last_light = None, None
        self._table = b""

    def _do(self, fn, quiet: bool = False) -> Future:
        def job():
            try:
                if self._pad is None:
                    self._pad = Keypad.open()
                return fn(self._pad)
            except Exception:
                self._close()
                self._retry_at = time.monotonic() + 3  # unplugged: don't hammer it
                raise
        f = self._ex.submit(job)
        if not quiet:
            f.add_done_callback(lambda f: f.exception() and logging.warning("keypad: %s", f.exception()))
        return f

    @staticmethod
    def status() -> dict:
        devs = enumerate_devices()
        return {"connected": bool(devs), "model": devs[0].name if devs else "", "usb": devs[0].usb_id if devs else ""}

    def custom(self, brightness: int) -> None:
        """Hand the LEDs to the host (mode 5). Writes flash, so only when it actually changes."""
        def job(pad):
            cur = pad.get_light()
            if (cur.mode, cur.brightness) != (5, brightness):
                pad.set_light(light_config("#ffffff", "custom", brightness))
                time.sleep(WRITE_GAP)
            self._last_light = ("custom", brightness)
            self._table = b""
        if self._last_light != ("custom", brightness) and time.monotonic() >= self._retry_at:
            self._last_light = ("custom", brightness)
            self._do(job, quiet=True)

    def frame(self, colors: list[tuple], knob: tuple, flip: bool) -> None:
        """Show one frame: colours for key positions 0-5 as the user sees them, plus the knob.

        Dropped when the previous frame is still being written, or for a few
        seconds after a failure (unplugged), so it never queues up or spams the log.
        """
        if self._frame_busy or time.monotonic() < self._retry_at or not isinstance(self._last_light, tuple):
            return
        table = bytearray(3 * (max(KNOB_LEDS) + 1))
        for p, c in enumerate(colors):
            i = 5 - p if flip else p
            table[3 * i:3 * i + 3] = bytes(c)
        for i in KNOB_LEDS:
            table[3 * i:3 * i + 3] = bytes(knob)
        table = bytes(table)
        if table == self._table and time.monotonic() - self._sent_at < 2:
            return  # unchanged; still resent every 2 s in case the firmware dropped a write
        self._frame_busy = True

        def job(pad):
            # ponytail: bulk per-key write [18] (sdcx's set_key_colors reads first and sends without a gap,
            # which this firmware drops). 57 bytes = 2 transfers, ~25 ms with the gaps. This is the path sdcx streams
            # effects on; only the light config [11] (custom()) is documented as persisted to flash.
            for n in range(0, len(table), 56):
                chunk = table[n:n + 56]
                size = 59 if n + 56 < len(table) or len(table) % 56 == 0 else len(table) % 56 + 3
                pad._send([CMD_SET_KEY_RGB_BULK, size, *u16le(n), 0, 0, 0, *chunk])
                time.sleep(WRITE_GAP)
            self._table, self._sent_at = table, time.monotonic()

        self._do(job, quiet=True).add_done_callback(lambda f: setattr(self, "_frame_busy", False))

    def off(self) -> None:
        if self._last_light == "off":
            return
        self._last_light = "off"
        self._do(lambda pad: pad.off())

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
