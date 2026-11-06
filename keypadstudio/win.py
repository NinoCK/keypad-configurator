"""Win32 glue via ctypes: key hook, synthetic input, tray icon, misc.

Everything that needs a message loop (hook, tray, lock notifications) lives on
the thread that calls `message_loop()`, which is the main thread.
"""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes as wt

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
wtsapi32 = ctypes.WinDLL("wtsapi32", use_last_error=True)

LRESULT = ctypes.c_ssize_t
ULONG_PTR = ctypes.c_size_t

# ---------------------------------------------------------------- input

class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD), ("time", wt.DWORD), ("dwExtraInfo", ULONG_PTR)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD), ("dwFlags", wt.DWORD), ("time", wt.DWORD), ("dwExtraInfo", ULONG_PTR)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("pad", ctypes.c_byte * 32)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wt.DWORD), ("u", _INPUTUNION)]


user32.SendInput.argtypes = [wt.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.VkKeyScanW.argtypes = [wt.WCHAR]
user32.VkKeyScanW.restype = ctypes.c_short

_KEYUP, _UNICODE, _EXTENDED = 0x2, 0x4, 0x1
_EXTENDED_VKS = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E, 0x5B, 0x5C, 0x6F, 0x90}

VK = {
    "CTRL": 0x11, "CONTROL": 0x11, "ALT": 0x12, "SHIFT": 0x10, "WIN": 0x5B, "META": 0x5B,
    "ENTER": 0x0D, "RETURN": 0x0D, "ESC": 0x1B, "ESCAPE": 0x1B, "TAB": 0x09, "SPACE": 0x20,
    "BACKSPACE": 0x08, "DELETE": 0x2E, "DEL": 0x2E, "INSERT": 0x2D, "HOME": 0x24, "END": 0x23,
    "PAGEUP": 0x21, "PAGEDOWN": 0x22, "ARROWLEFT": 0x25, "ARROWUP": 0x26, "ARROWRIGHT": 0x27,
    "ARROWDOWN": 0x28, "LEFT": 0x25, "UP": 0x26, "RIGHT": 0x27, "DOWN": 0x28,
    "PRINTSCREEN": 0x2C, "CAPSLOCK": 0x14, "PAUSE": 0x13, "CONTEXTMENU": 0x5D,
}
VK.update({f"F{n}": 0x6F + n for n in range(1, 25)})
VK_VOLUME_MUTE, VK_VOLUME_DOWN, VK_VOLUME_UP = 0xAD, 0xAE, 0xAF
VK_MEDIA = {"next": 0xB0, "prev": 0xB1, "play_pause": 0xB3, "mute": VK_VOLUME_MUTE}


def parse_combo(text: str) -> list[int]:
    """'Ctrl+Shift+S' -> [VK_CONTROL, VK_SHIFT, 'S']. Raises ValueError on unknown names."""
    vks = []
    for part in [p.strip() for p in text.split("+")] if text.strip() != "+" else ["+"]:
        if not part:
            continue
        name = part.upper()
        if name in VK:
            vks.append(VK[name])
        elif len(part) == 1:
            if part.isalnum() and part.isascii():
                vks.append(ord(name))
            else:
                scan = user32.VkKeyScanW(part)
                if scan == -1:
                    raise ValueError(f"no key for {part!r} on this keyboard layout")
                vks.append(scan & 0xFF)
        else:
            raise ValueError(f"unknown key name {part!r}")
    if not vks:
        raise ValueError("empty shortcut")
    return vks


def _key(vk: int, up: bool = False) -> INPUT:
    i = INPUT(type=1)
    i.u.ki = KEYBDINPUT(vk, 0, (_KEYUP if up else 0) | (_EXTENDED if vk in _EXTENDED_VKS else 0), 0, 0)
    return i


def _send(inputs: list[INPUT]) -> None:
    if inputs:
        arr = (INPUT * len(inputs))(*inputs)
        user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT))


def tap(vk: int, times: int = 1) -> None:
    _send([_key(vk), _key(vk, True)] * times)


def send_combo(vks: list[int]) -> None:
    _send([_key(v) for v in vks] + [_key(v, True) for v in reversed(vks)])


def type_text(text: str) -> None:
    inputs = []
    for ch in text.replace("\r\n", "\n"):
        if ch == "\n":
            inputs += [_key(0x0D), _key(0x0D, True)]
            continue
        data = ch.encode("utf-16-le")
        for unit in (int.from_bytes(data[i:i + 2], "little") for i in range(0, len(data), 2)):
            for up in (False, True):
                i = INPUT(type=1)
                i.u.ki = KEYBDINPUT(0, unit, _UNICODE | (_KEYUP if up else 0), 0, 0)
                inputs.append(i)
    _send(inputs)


def wheel(delta: int, ctrl: bool = False) -> None:
    m = INPUT(type=0)
    m.u.mi = MOUSEINPUT(0, 0, delta & 0xFFFFFFFF, 0x0800, 0, 0)  # MOUSEEVENTF_WHEEL
    _send(([_key(0x11)] if ctrl else []) + [m] + ([_key(0x11, True)] if ctrl else []))


# ---------------------------------------------------------------- key hook

class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wt.DWORD), ("scanCode", wt.DWORD), ("flags", wt.DWORD), ("time", wt.DWORD), ("dwExtraInfo", ULONG_PTR)]


HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wt.WPARAM, wt.LPARAM)
user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wt.HINSTANCE, wt.DWORD]
user32.SetWindowsHookExW.restype = wt.HHOOK
user32.CallNextHookEx.argtypes = [wt.HHOOK, ctypes.c_int, wt.WPARAM, wt.LPARAM]
user32.CallNextHookEx.restype = LRESULT
user32.UnhookWindowsHookEx.argtypes = [wt.HHOOK]

VK_F13 = 0x7C
KEY_COUNT = 9  # F13..F21


class KeyHook:
    """Swallows F13-F21 system-wide and reports (index 0-8, is_down)."""

    def __init__(self, handler):
        self._handler = handler
        self._proc = HOOKPROC(self._callback)  # keep a reference or it gets collected
        self._hook = user32.SetWindowsHookExW(13, self._proc, None, 0)  # WH_KEYBOARD_LL
        if not self._hook:
            raise ctypes.WinError(ctypes.get_last_error())

    def _callback(self, code, wparam, lparam):
        if code == 0:
            kb = KBDLLHOOKSTRUCT.from_address(lparam)
            index = kb.vkCode - VK_F13
            if 0 <= index < KEY_COUNT:
                self._handler(index, wparam in (0x100, 0x104))  # WM_KEYDOWN, WM_SYSKEYDOWN
                return 1
        return user32.CallNextHookEx(None, code, wparam, lparam)

    def close(self):
        if self._hook:
            user32.UnhookWindowsHookEx(self._hook)
            self._hook = None


# ---------------------------------------------------------------- tray window

WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wt.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON), ("hCursor", wt.HANDLE), ("hbrBackground", wt.HBRUSH),
                ("lpszMenuName", wt.LPCWSTR), ("lpszClassName", wt.LPCWSTR)]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("hWnd", wt.HWND), ("uID", wt.UINT), ("uFlags", wt.UINT),
                ("uCallbackMessage", wt.UINT), ("hIcon", wt.HICON), ("szTip", wt.WCHAR * 128),
                ("dwState", wt.DWORD), ("dwStateMask", wt.DWORD), ("szInfo", wt.WCHAR * 256),
                ("uVersion", wt.UINT), ("szInfoTitle", wt.WCHAR * 64), ("dwInfoFlags", wt.DWORD),
                ("guidItem", ctypes.c_byte * 16), ("hBalloonIcon", wt.HICON)]


class ICONINFO(ctypes.Structure):
    _fields_ = [("fIcon", wt.BOOL), ("xHotspot", wt.DWORD), ("yHotspot", wt.DWORD), ("hbmMask", wt.HBITMAP), ("hbmColor", wt.HBITMAP)]


kernel32.GetModuleHandleW.restype = wt.HMODULE
user32.DefWindowProcW.argtypes =[wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.DefWindowProcW.restype = LRESULT
user32.CreateWindowExW.argtypes = [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID]
user32.CreateWindowExW.restype = wt.HWND
user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
user32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.CreatePopupMenu.restype = wt.HMENU
user32.AppendMenuW.argtypes = [wt.HMENU, wt.UINT, ctypes.c_size_t, wt.LPCWSTR]
user32.TrackPopupMenu.argtypes = [wt.HMENU, wt.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.HWND, wt.LPVOID]
user32.DestroyMenu.argtypes = [wt.HMENU]
user32.SetForegroundWindow.argtypes = [wt.HWND]
user32.DestroyWindow.argtypes = [wt.HWND]
user32.CreateIconIndirect.argtypes = [ctypes.POINTER(ICONINFO)]
user32.CreateIconIndirect.restype = wt.HICON
user32.DestroyIcon.argtypes = [wt.HICON]
gdi32.CreateBitmap.argtypes = [ctypes.c_int, ctypes.c_int, wt.UINT, wt.UINT, ctypes.c_void_p]
gdi32.CreateBitmap.restype = wt.HBITMAP
gdi32.DeleteObject.argtypes = [wt.HANDLE]
shell32.Shell_NotifyIconW.argtypes = [wt.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
wtsapi32.WTSRegisterSessionNotification.argtypes = [wt.HWND, wt.DWORD]

WM_TRAY = 0x8001
WM_WTSSESSION_CHANGE = 0x2B1


def make_icon(hex_color: str, size: int = 32) -> int:
    """A filled circle in `hex_color` with a light ring, as an HICON."""
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4)) if len(h) == 6 else (128, 128, 128)
    px = bytearray()
    c = (size - 1) / 2
    for y in range(size):
        for x in range(size):
            d = ((x - c) ** 2 + (y - c) ** 2) ** 0.5
            if d <= c - 3:
                px += bytes((b, g, r, 255))
            elif d <= c:
                px += bytes((235, 235, 235, 255))
            else:
                px += bytes(4)
    color = gdi32.CreateBitmap(size, size, 1, 32, bytes(px))
    mask = gdi32.CreateBitmap(size, size, 1, 1, bytes(size * size // 8))
    icon = user32.CreateIconIndirect(ctypes.byref(ICONINFO(True, 0, 0, mask, color)))
    gdi32.DeleteObject(color)
    gdi32.DeleteObject(mask)
    return icon


class Tray:
    """Hidden window owning the tray icon; also receives lock/unlock notifications.

    `menu()` returns [(id, text, checked) | None for separator]; `on_command(id)`
    runs the picked one. `on_lock(bool)` fires on session lock/unlock.
    """

    def __init__(self, tooltip, on_open, menu, on_command, on_lock):
        self.on_open, self.menu, self.on_command, self.on_lock = on_open, menu, on_command, on_lock
        self._taskbar_created = user32.RegisterWindowMessageW("TaskbarCreated")
        self._icon = None
        self._tip = tooltip
        self._color = "#808080"
        self._proc = WNDPROC(self._wndproc)
        hinst = kernel32.GetModuleHandleW(None)
        wc = WNDCLASSW(lpfnWndProc=self._proc, hInstance=hinst, lpszClassName="KeypadStudioTray")
        user32.RegisterClassW(ctypes.byref(wc))
        self.hwnd = user32.CreateWindowExW(0, "KeypadStudioTray", "Keypad Studio", 0, 0, 0, 0, 0, None, None, hinst, None)
        wtsapi32.WTSRegisterSessionNotification(self.hwnd, 0)
        self._notify(0)  # NIM_ADD

    def _data(self):
        n = NOTIFYICONDATAW(cbSize=ctypes.sizeof(NOTIFYICONDATAW), hWnd=self.hwnd, uID=1,
                            uFlags=0x1 | 0x2 | 0x4, uCallbackMessage=WM_TRAY)  # MESSAGE | ICON | TIP
        if self._icon is None:
            self._icon = make_icon(self._color)
        n.hIcon = self._icon
        n.szTip = self._tip[:127]
        return n

    def _notify(self, op):
        shell32.Shell_NotifyIconW(op, ctypes.byref(self._data()))

    def update(self, tooltip: str, color: str) -> None:
        """Safe from any thread."""
        old = self._icon
        self._tip, self._color, self._icon = tooltip, color, make_icon(color)
        self._notify(1)  # NIM_MODIFY
        if old:
            user32.DestroyIcon(old)

    def _show_menu(self):
        items = self.menu()
        m = user32.CreatePopupMenu()
        for item in items:
            if item is None:
                user32.AppendMenuW(m, 0x800, 0, None)  # MF_SEPARATOR
            else:
                cid, text, checked = item
                user32.AppendMenuW(m, 0x8 if checked else 0, cid, text)  # MF_CHECKED
        pt = wt.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        user32.SetForegroundWindow(self.hwnd)
        cmd = user32.TrackPopupMenu(m, 0x100 | 0x80, pt.x, pt.y, 0, self.hwnd, None)  # RETURNCMD | NONOTIFY
        user32.PostMessageW(self.hwnd, 0, 0, 0)
        user32.DestroyMenu(m)
        if cmd:
            self.on_command(cmd)

    def _wndproc(self, hwnd, msg, wparam, lparam):
        try:
            if msg == WM_TRAY:
                event = lparam & 0xFFFF
                if event in (0x202, 0x203):  # LBUTTONUP, LBUTTONDBLCLK
                    self.on_open()
                elif event == 0x205:  # RBUTTONUP
                    self._show_menu()
                return 0
            if msg == WM_WTSSESSION_CHANGE and wparam in (7, 8):  # WTS_SESSION_LOCK / UNLOCK
                self.on_lock(wparam == 7)
                return 0
            if msg == self._taskbar_created:  # Explorer restarted
                self._notify(0)
                return 0
            if msg == 0x0002:  # WM_DESTROY
                self._notify(2)  # NIM_DELETE
                user32.PostQuitMessage(0)
                return 0
        except Exception:  # never let an exception escape into Win32
            import logging
            logging.exception("tray window procedure")
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def quit(self):
        """Safe from any thread: ends message_loop()."""
        user32.PostMessageW(self.hwnd, 0x0010, 0, 0)  # WM_CLOSE -> DestroyWindow -> WM_DESTROY


def message_loop() -> None:
    msg = wt.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))


# ---------------------------------------------------------------- misc

kernel32.OpenProcess.restype = wt.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)]
kernel32.CloseHandle.argtypes = [wt.HANDLE]
user32.GetForegroundWindow.restype = wt.HWND
user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]


def foreground_exe() -> str:
    """Lower-case file name of the foreground window's program, or ''."""
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(user32.GetForegroundWindow(), ctypes.byref(pid))
    return exe_of_pid(pid.value)


def exe_of_pid(pid: int) -> str:
    """Lower-case file name of a process's program, or ''."""
    if not pid:
        return ""
    h = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return ""
    try:
        buf, n = ctypes.create_unicode_buffer(1024), wt.DWORD(1024)
        if not kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
            return ""
        return os.path.basename(buf.value).lower()
    finally:
        kernel32.CloseHandle(h)


def single_instance(name: str) -> bool:
    """True if this is the first instance. The mutex lives as long as the process."""
    kernel32.CreateMutexW.restype = wt.HANDLE
    kernel32.CreateMutexW(None, False, name)
    return ctypes.get_last_error() != 183  # ERROR_ALREADY_EXISTS


def dpi_aware() -> None:
    try:
        user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # PER_MONITOR_AWARE_V2
    except Exception:
        pass


# Credential Manager (generic credential), so the HA token is not in a plain file.

class CREDENTIALW(ctypes.Structure):
    _fields_ = [("Flags", wt.DWORD), ("Type", wt.DWORD), ("TargetName", wt.LPWSTR), ("Comment", wt.LPWSTR),
                ("LastWritten", wt.FILETIME), ("CredentialBlobSize", wt.DWORD), ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
                ("Persist", wt.DWORD), ("AttributeCount", wt.DWORD), ("Attributes", ctypes.c_void_p),
                ("TargetAlias", wt.LPWSTR), ("UserName", wt.LPWSTR)]


advapi32.CredReadW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.POINTER(ctypes.POINTER(CREDENTIALW))]
advapi32.CredWriteW.argtypes = [ctypes.POINTER(CREDENTIALW), wt.DWORD]
advapi32.CredDeleteW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD]
advapi32.CredFree.argtypes = [ctypes.c_void_p]


def cred_get(target: str) -> str:
    p = ctypes.POINTER(CREDENTIALW)()
    if not advapi32.CredReadW(target, 1, 0, ctypes.byref(p)):
        return ""
    try:
        c = p.contents
        return ctypes.string_at(c.CredentialBlob, c.CredentialBlobSize).decode("utf-16-le")
    finally:
        advapi32.CredFree(p)


def cred_set(target: str, secret: str) -> None:
    if not secret:
        advapi32.CredDeleteW(target, 1, 0)
        return
    blob = secret.encode("utf-16-le")
    buf = ctypes.create_string_buffer(blob, len(blob))
    c = CREDENTIALW(Type=1, TargetName=target, CredentialBlobSize=len(blob),
                    CredentialBlob=ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)), Persist=2, UserName="KeypadStudio")
    if not advapi32.CredWriteW(ctypes.byref(c), 0):
        raise ctypes.WinError(ctypes.get_last_error())
