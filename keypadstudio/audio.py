"""Windows Core Audio through raw COM vtable calls (no comtypes).

- app_volume(): change one program's volume (mixer session), like Spotify only
- toggle_mic(): mute/unmute the default microphone
- output_devices() / set_default_output(): switch speakers <-> headphones

Default-device switching uses IPolicyConfig, which Windows does not document
but every audio-switcher tool relies on; it has been stable since Windows 7.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes as wt

from . import win

ole32 = ctypes.OleDLL("ole32")


class GUID(ctypes.Structure):
    _fields_ = [("d1", wt.DWORD), ("d2", wt.WORD), ("d3", wt.WORD), ("d4", ctypes.c_ubyte * 8)]

    def __init__(self, text: str = ""):
        super().__init__()
        if text:
            ole32.CLSIDFromString(ctypes.c_wchar_p("{" + text + "}"), ctypes.byref(self))


class PROPERTYKEY(ctypes.Structure):
    _fields_ = [("fmtid", GUID), ("pid", wt.DWORD)]


class PROPVARIANT(ctypes.Structure):
    _fields_ = [("vt", wt.USHORT), ("r1", wt.USHORT), ("r2", wt.USHORT), ("r3", wt.USHORT), ("ptr", ctypes.c_void_p), ("pad", ctypes.c_void_p)]


CLSID_MMDeviceEnumerator = GUID("BCDE0395-E52F-467C-8E3D-C4579291692E")
IID_IMMDeviceEnumerator = GUID("A95664D2-9614-4F35-A746-DE8DB63617E6")
IID_IAudioEndpointVolume = GUID("5CDF2C82-841E-4546-9722-0CF74078229A")
IID_IAudioSessionManager2 = GUID("77AA99A0-1BD6-484F-8BC7-2C654C9A9B6F")
IID_IAudioSessionControl2 = GUID("BFB7FF88-7239-4FC9-8FA2-07C950BE9C6D")
IID_ISimpleAudioVolume = GUID("87CE5498-68D6-44E5-9215-6DA47EF883D8")
CLSID_PolicyConfigClient = GUID("870AF99C-171D-4F9E-AF0D-E63DF40C2BC9")
IID_IPolicyConfig = GUID("F8679F50-850A-41CF-9C72-430F290290C8")
PKEY_Device_FriendlyName = PROPERTYKEY(GUID("A45C254E-DF1C-4EFD-8020-67D146A850E0"), 14)

RENDER, CAPTURE = 0, 1
CLSCTX_ALL = 0x17


def _call(ptr, index: int, *args, argtypes=()):
    """Call method `index` of the COM object at `ptr`; raises OSError on a failed HRESULT."""
    vtbl = ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    fn = ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, *argtypes)(vtbl[index])
    return fn(ptr, *args)


def _release(ptr) -> None:
    if ptr:
        vtbl = ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        ctypes.WINFUNCTYPE(wt.ULONG, ctypes.c_void_p)(vtbl[2])(ptr)


def _out(index, obj, *args, argtypes=()):
    """Call a method whose last parameter is an out-pointer to an interface."""
    p = ctypes.c_void_p()
    _call(obj, index, *args, ctypes.byref(p), argtypes=(*argtypes, ctypes.POINTER(ctypes.c_void_p)))
    return p.value


def _create(clsid: GUID, iid: GUID) -> int:
    p = ctypes.c_void_p()
    ole32.CoCreateInstance(ctypes.byref(clsid), None, CLSCTX_ALL, ctypes.byref(iid), ctypes.byref(p))
    return p.value


class _Com:
    def __enter__(self):
        try:
            ole32.CoInitializeEx(None, 0)  # MTA; harmless if the thread already has COM
        except OSError:
            pass
        self.held = []
        self.enum = self.keep(_create(CLSID_MMDeviceEnumerator, IID_IMMDeviceEnumerator))
        return self

    def keep(self, ptr):
        self.held.append(ptr)
        return ptr

    def __exit__(self, *exc):
        for p in reversed(self.held):
            _release(p)

    def default(self, flow: int, role: int = 0):
        return self.keep(_out(4, self.enum, flow, role, argtypes=(ctypes.c_int, ctypes.c_int)))

    def activate(self, device, iid: GUID):
        return self.keep(_out(3, device, ctypes.byref(iid), CLSCTX_ALL, None,
                              argtypes=(ctypes.POINTER(GUID), wt.DWORD, ctypes.c_void_p)))

    def query(self, obj, iid: GUID):
        return self.keep(_out(0, obj, ctypes.byref(iid), argtypes=(ctypes.POINTER(GUID),)))

    @staticmethod
    def device_id(device) -> str:
        s = ctypes.c_wchar_p()
        _call(device, 5, ctypes.byref(s), argtypes=(ctypes.POINTER(ctypes.c_wchar_p),))
        value = s.value
        ctypes.windll.ole32.CoTaskMemFree(s)  # returns void, so not through OleDLL
        return value

    def name(self, device) -> str:
        store = self.keep(_out(4, device, 0, argtypes=(wt.DWORD,)))  # OpenPropertyStore(STGM_READ)
        pv = PROPVARIANT()
        _call(store, 5, ctypes.byref(PKEY_Device_FriendlyName), ctypes.byref(pv),
              argtypes=(ctypes.POINTER(PROPERTYKEY), ctypes.POINTER(PROPVARIANT)))
        value = ctypes.wstring_at(pv.ptr) if pv.vt == 31 and pv.ptr else ""  # VT_LPWSTR
        ole32.PropVariantClear(ctypes.byref(pv))
        return value

    def devices(self, flow: int) -> list[tuple[str, str]]:
        coll = self.keep(_out(3, self.enum, flow, 1, argtypes=(ctypes.c_int, wt.DWORD)))  # DEVICE_STATE_ACTIVE
        n = wt.UINT()
        _call(coll, 3, ctypes.byref(n), argtypes=(ctypes.POINTER(wt.UINT),))
        out = []
        for i in range(n.value):
            dev = self.keep(_out(4, coll, i, argtypes=(wt.UINT,)))
            out.append((self.device_id(dev), self.name(dev)))
        return out


# ---------------------------------------------------------------- public

def output_devices() -> list[str]:
    with _Com() as c:
        return [name for _, name in c.devices(RENDER)]


def default_output() -> str:
    with _Com() as c:
        return c.name(c.default(RENDER))


def set_default_output(name_part: str) -> str:
    """Make the first active output whose name contains `name_part` the default. Returns its full name."""
    with _Com() as c:
        match = next(((i, n) for i, n in c.devices(RENDER) if name_part.lower() in n.lower()), None)
        if not match:
            raise LookupError(f"no audio output matching {name_part!r}")
        policy = c.keep(_create(CLSID_PolicyConfigClient, IID_IPolicyConfig))
        for role in (0, 1, 2):  # console, multimedia, communications
            _call(policy, 13, match[0], role, argtypes=(wt.LPCWSTR, ctypes.c_int))
        return match[1]


def cycle_output(names: list[str]) -> str:
    """Switch to the entry after the current default in `names` (substring matches)."""
    names = [n.strip() for n in names if n.strip()]
    if not names:
        raise LookupError("no audio outputs listed")
    current = default_output().lower()
    idx = next((i for i, n in enumerate(names) if n.lower() in current), -1)
    return set_default_output(names[(idx + 1) % len(names)])


def toggle_mic() -> bool:
    """Toggle the default microphone's mute. Returns True if now muted."""
    with _Com() as c:
        vol = c.activate(c.default(CAPTURE, 2), IID_IAudioEndpointVolume)  # communications mic
        muted = wt.BOOL()
        _call(vol, 15, ctypes.byref(muted), argtypes=(ctypes.POINTER(wt.BOOL),))
        _call(vol, 14, not muted.value, None, argtypes=(wt.BOOL, ctypes.c_void_p))
        return not muted.value


def _sessions(c: _Com):
    """Yield (exe name, IAudioSessionControl) for each mixer session on the default output."""
    mgr = c.activate(c.default(RENDER), IID_IAudioSessionManager2)
    sessions = c.keep(_out(5, mgr))  # GetSessionEnumerator
    n = ctypes.c_int()
    _call(sessions, 3, ctypes.byref(n), argtypes=(ctypes.POINTER(ctypes.c_int),))
    for i in range(n.value):
        ctl = c.keep(_out(4, sessions, i, argtypes=(ctypes.c_int,)))
        pid = wt.DWORD()
        _call(c.query(ctl, IID_IAudioSessionControl2), 14, ctypes.byref(pid), argtypes=(ctypes.POINTER(wt.DWORD),))
        yield win.exe_of_pid(pid.value), ctl


def session_apps() -> list[str]:
    """Programs that currently have a volume slider in the mixer."""
    with _Com() as c:
        return sorted({name for name, _ in _sessions(c) if name})


def app_volume(exe: str, delta: float) -> float | None:
    """Change every mixer session of `exe` (e.g. 'spotify.exe') by `delta` (0-1 scale).

    Returns the new volume, or None if that program is not playing audio.
    """
    exe = exe.lower() if exe.lower().endswith(".exe") else exe.lower() + ".exe"
    result = None
    with _Com() as c:
        for name, ctl in _sessions(c):
            if name != exe:
                continue
            vol = c.query(ctl, IID_ISimpleAudioVolume)
            level = ctypes.c_float()
            _call(vol, 4, ctypes.byref(level), argtypes=(ctypes.POINTER(ctypes.c_float),))
            result = min(1.0, max(0.0, level.value + delta))
            _call(vol, 3, ctypes.c_float(result), None, argtypes=(ctypes.c_float, ctypes.c_void_p))
    return result
