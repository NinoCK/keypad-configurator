"""Keypad Studio: tray app that turns the HCY-K006 keypad into profiles of actions.

Run:  pythonw keypad-studio.pyw            (tray + opens settings)
      pythonw keypad-studio.pyw --background (tray only, used at sign-in)
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "sdcx-keypad"))

from . import config, win  # noqa: E402


def open_ui(port: int, secret: str) -> None:
    url = f"http://127.0.0.1:{port}/#k={secret}"
    for exe in (r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe", r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
                r"%LocalAppData%\Microsoft\Edge\Application\msedge.exe"):
        exe = os.path.expandvars(exe)
        if os.path.exists(exe):
            subprocess.Popen([exe, f"--app={url}", "--window-size=1200,840"])
            return
    webbrowser.open(url)


class App:
    def __init__(self):
        from .device import Device
        from .engine import Engine
        from .osd import Osd
        self._token = win.cred_get(config.TOKEN_TARGET)
        self.device, self.osd = Device(), Osd()
        self.engine = Engine(config.load(), self.device, self.osd, self.token)

    def token(self) -> str:
        return self._token

    def set_token(self, token: str) -> None:
        win.cred_set(config.TOKEN_TARGET, token)
        self._token = token
        self.engine.entities = []


def main() -> None:
    config.APP_DIR.mkdir(parents=True, exist_ok=True)
    handlers = [logging.FileHandler(config.LOG_PATH, encoding="utf-8")]
    if sys.stderr:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=handlers)
    background = "--background" in sys.argv

    if not win.single_instance("Local\\KeypadStudio"):
        try:  # already running: just open its settings window
            inst = json.loads(config.INSTANCE_PATH.read_text())
            if not background:
                open_ui(inst["port"], inst["secret"])
        except (OSError, ValueError, KeyError):
            logging.error("another instance is running but instance.json is unreadable")
        return

    win.dpi_aware()
    from . import server
    app = App()
    eng = app.engine
    port, secret = server.start(app)
    config.INSTANCE_PATH.write_text(json.dumps({"port": port, "secret": secret}))

    PAUSE, OPEN, QUIT, PROFILE0 = 1, 2, 3, 100

    def menu():
        items = [(OPEN, "Open settings", False), None]
        items += [(PROFILE0 + i, p["name"], p["id"] == eng.cfg["active"]) for i, p in enumerate(eng.cfg["profiles"])]
        return items + [None, (PAUSE, "Pause keypad", eng.paused), (QUIT, "Quit", False)]

    def command(cid):
        if cid == OPEN:
            open_ui(port, secret)
        elif cid == PAUSE:
            eng.paused = not eng.paused
            refresh()
        elif cid == QUIT:
            tray.quit()
        elif cid >= PROFILE0 and cid - PROFILE0 < len(eng.cfg["profiles"]):
            eng.activate(eng.cfg["profiles"][cid - PROFILE0]["id"])

    tray = win.Tray("Keypad Studio", lambda: open_ui(port, secret), menu, command, eng.set_locked)

    def refresh():
        p = eng.profile()
        tray.update(f"Keypad Studio: {p['name']}" + (" (paused)" if eng.paused else ""), "#808080" if eng.paused else p["color"])

    eng.on_change = refresh
    refresh()
    hook = win.KeyHook(eng.on_key)
    eng.start()
    logging.info("started on port %d, profile %s", port, eng.cfg["active"])
    if not background:
        open_ui(port, secret)
    try:
        win.message_loop()
    finally:
        hook.close()
        try:
            config.INSTANCE_PATH.unlink()
        except OSError:
            pass
        logging.info("stopped")


if __name__ == "__main__":
    main()
