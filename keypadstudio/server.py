"""Local HTTP API for the settings window (127.0.0.1 only).

The API can launch programs, so it is locked down: every /api request needs the
per-run secret in an X-Key header (a custom header also means other websites
cannot send it without a CORS preflight, which this server never approves), and
the Host header must be this server's own address, which defeats DNS rebinding.
"""

from __future__ import annotations

import hmac
import json
import logging
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from . import actions, config, lighting, win

UI_PATH = Path(__file__).resolve().parent / "ui.html"
PREFERRED_PORT = 47613


class ApiError(Exception):
    pass


def make_routes(app):
    eng = app.engine

    def state(_):
        return {"config": eng.cfg, "status": {**app.device.status(), "active": eng.cfg["active"], "paused": eng.paused,
                                               "tokenSet": bool(app.token()), "logPath": str(config.LOG_PATH),
                                               "look": eng.look(), "liveError": eng.live_error},
                "lighting": {"climate": lighting.DEFAULT_CLIMATE, "air": lighting.DEFAULT_AIR, "modes": lighting.MODE_NAMES}}

    def put_config(body):
        try:
            eng.replace_config(body)
        except config.ConfigError as e:
            raise ApiError(str(e)) from e
        return {"ok": True}

    def token(body):
        body = body or {}
        value = str(body.get("token", "")).strip()
        if not value and not body.get("remove"):
            raise ApiError("paste a token first")
        app.set_token("" if body.get("remove") else value)
        logging.info("Home Assistant token %s", "removed" if body.get("remove") else "saved")
        return {"ok": True}

    def ha_test(_):
        try:
            ha = eng.ha()
            msg = ha.ping()
            eng.entities = ha.entities()
        except actions.ActionError as e:
            raise ApiError(str(e)) from e
        return {"message": f"Connected ({msg}). Found {len(eng.entities)} entities.", "entities": eng.entities}

    def entities(_):
        if not eng.entities and app.token():
            try:
                eng.entities = eng.ha().entities()
            except actions.ActionError:
                pass
        return {"entities": eng.entities}

    def run(body):
        p = eng.profile(body.get("profile"))
        a = p["keys"][int(body["key"])][body.get("slot", "tap")]
        try:
            actions.run(a, eng.ha, eng.activate, eng.notify)
        except Exception as e:
            raise ApiError(str(e)) from e
        return {"ok": True}

    def activate(body):
        eng.activate(str(body.get("id")))
        return {"ok": True}

    def device_job(fut):
        try:
            return {"message": fut.result(timeout=20)}
        except Exception as e:
            raise ApiError(str(e)) from e

    def browse(_):
        return {"path": app.osd.ask_file()}

    def audio_info(_):
        from . import audio
        try:
            return {"outputs": audio.output_devices(), "apps": audio.session_apps()}
        except OSError as e:
            logging.warning("audio: %s", e)
            return {"outputs": [], "apps": []}

    return {
        ("GET", "/api/state"): state,
        ("PUT", "/api/config"): put_config,
        ("POST", "/api/token"): token,
        ("POST", "/api/ha/test"): ha_test,
        ("GET", "/api/ha/entities"): entities,
        ("POST", "/api/run"): run,
        ("POST", "/api/activate"): activate,
        ("POST", "/api/program"): lambda _: device_job(app.device.program()),
        ("POST", "/api/restore"): lambda _: device_job(app.device.restore()),
        ("POST", "/api/browse"): browse,
        ("GET", "/api/audio"): audio_info,
    }


def start(app) -> tuple[int, str]:
    secret = secrets.token_urlsafe(24)
    routes = make_routes(app)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _reply(self, code: int, body: bytes, ctype: str = "application/json") -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj) -> None:
            self._reply(code, json.dumps(obj).encode())

        def _dispatch(self, method: str) -> None:
            port = self.server.server_address[1]
            if self.headers.get("Host") not in (f"127.0.0.1:{port}", f"localhost:{port}"):
                return self._json(403, {"error": "bad host"})
            path = urlparse(self.path).path
            if method == "GET" and path in ("/", "/index.html"):
                return self._reply(200, UI_PATH.read_bytes(), "text/html; charset=utf-8")
            if not path.startswith("/api/"):
                return self._json(404, {"error": "not found"})
            if not hmac.compare_digest(self.headers.get("X-Key", ""), secret):
                return self._json(403, {"error": "missing or wrong key; open settings from the tray icon"})
            route = routes.get((method, path))
            if not route:
                return self._json(404, {"error": "not found"})
            try:
                length = min(int(self.headers.get("Content-Length") or 0), 1 << 20)
                body = json.loads(self.rfile.read(length)) if length else None
                self._json(200, route(body))
            except ApiError as e:
                self._json(400, {"error": str(e)})
            except Exception as e:
                logging.exception("api %s %s", method, path)
                self._json(500, {"error": str(e)})

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def do_PUT(self):
            self._dispatch("PUT")

    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", PREFERRED_PORT), Handler)
    except OSError:
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True, name="http").start()
    return httpd.server_address[1], secret
