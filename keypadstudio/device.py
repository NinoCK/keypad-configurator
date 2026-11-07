"""Keypad access through sdcx, serialised on one worker thread.

The pad stays open between operations; any failure closes it, so a replugged
keypad is reopened on the next operation.

Lighting uses the firmware's own effects (light config [11]). Host-drawn per-key
frames were tried and dropped: every write stalls the controller for ~4 ms
(measured) and the LEDs visibly glitch (bright flashes, random colours), so the
app writes only when the look actually changes and lets the keypad animate.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

from sdcx import Keypad, enumerate_devices
from sdcx.protocol import LightConfig

from .config import BACKUP_PATH

# Key index on the device -> key it should send. Index 16-18 is the knob.
APP_KEYMAP = {0: "f13", 1: "f14", 2: "f15", 3: "f16", 4: "f17", 5: "f18", 16: "f19", 17: "f20", 18: "f21"}
LED_MODES = {"off": 0, "steady": 1, "breath": 2, "press": 3, "tidal": 4}
MIN_WRITE_GAP = 1.0  # ponytail: set_light persists to flash; at most one write per second, latest wins
ORIGINAL_BACKUP = Path(__file__).resolve().parent.parent / "keymap-backup.json"  # saved before this app existed


def light_config(hex_color: str, mode: str, brightness: int, speed: int = 2, level: float = 1.0) -> LightConfig:
    import colorsys
    if hex_color == "rainbow":  # firmware palette: colour=0, v still scales brightness
        m = LED_MODES.get(mode, 1)
        if brightness <= 0 or m == 0:
            return LightConfig(type=1, mode=0)
        return LightConfig(type=1, mode=m, brightness=brightness, speed=speed, color=0, h=0, s=100, v=max(1, round(100 * level)))
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    hh, ss, vv = colorsys.rgb_to_hsv(r, g, b)
    m = LED_MODES.get(mode, 1)
    if brightness <= 0 or m == 0:
        return LightConfig(type=1, mode=0)
    return LightConfig(type=1, mode=m, brightness=brightness, speed=speed, color=1,
                       h=round(hh * 360) % 360, s=round(ss * 100), v=max(1, round(vv * 100 * level)))


class Device:
    def __init__(self):
        self._ex = ThreadPoolExecutor(1, thread_name_prefix="keypad")
        self._pad = None
        self._lock = threading.Lock()
        self._want: LightConfig | None = None   # latest requested light
        self._shown: LightConfig | None = None  # what the keypad has
        self._queued, self._written_at, self._retry_at = False, 0.0, 0.0
        self._status, self._status_at = {}, 0.0

    def _close(self) -> None:
        if self._pad:
            try:
                self._pad.close()
            except Exception:
                pass
        self._pad, self._shown = None, None

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

    def status(self) -> dict:
        # Enumerating asks the keypad for its name over USB; the settings window polls,
        # so answer from a short cache instead of interrupting the controller every 1.5 s.
        if time.monotonic() - self._status_at > 10:
            devs = enumerate_devices()
            self._status = {"connected": bool(devs), "model": devs[0].name if devs else "", "usb": devs[0].usb_id if devs else ""}
            self._status_at = time.monotonic()
        return self._status

    def light(self, cfg: LightConfig) -> None:
        """Ask for a light config. Cheap to call often: writes only on change, at most once per MIN_WRITE_GAP."""
        with self._lock:
            self._want = cfg
            if self._queued or cfg == self._shown or time.monotonic() < self._retry_at:
                return
            self._queued = True

        def job(pad):
            time.sleep(max(0.0, self._written_at + MIN_WRITE_GAP - time.monotonic()))
            with self._lock:
                cfg, self._queued = self._want, False
            if cfg != self._shown:
                pad.set_light(cfg)
                self._shown, self._written_at = cfg, time.monotonic()

        def failed(f):
            if f.exception():
                with self._lock:
                    self._queued = False
        self._do(job, quiet=True).add_done_callback(failed)

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
