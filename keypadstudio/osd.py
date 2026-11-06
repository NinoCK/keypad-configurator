"""On-screen profile popup and the native file picker, on one tkinter thread."""

from __future__ import annotations

import queue
import threading


class Osd:
    def __init__(self):
        self._q: queue.Queue = queue.Queue()
        threading.Thread(target=self._run, daemon=True, name="osd").start()

    def show(self, text: str, color: str, sticky: bool = False, ms: int = 1300) -> None:
        self._q.put(("show", text, color, sticky, ms))

    def ask_file(self) -> str:
        done, box = threading.Event(), {}
        self._q.put(("file", done, box))
        done.wait()
        return box.get("path", "")

    def _run(self) -> None:
        import tkinter as tk
        from tkinter import filedialog, font as tkfont

        import ctypes
        import winreg
        from ctypes import wintypes as wt

        root = tk.Tk()
        root.withdraw()
        try:  # flyouts follow the *system* theme, not the app theme
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
                light = winreg.QueryValueEx(k, "SystemUsesLightTheme")[0] == 1
        except OSError:
            light = False
        bg, fg, border = ("#f9f9f9", "#1a1a1a", 0xE5E5E5) if light else ("#2c2c2c", "#ffffff", 0x3D3D3D)
        font = "Segoe UI Variable Display" if "Segoe UI Variable Display" in tkfont.families(root) else "Segoe UI"

        pop = tk.Toplevel(root)
        pop.overrideredirect(True)
        pop.attributes("-topmost", True)
        pop.configure(bg=bg)
        dot = tk.Label(pop, text="●", bg=bg, font=(font, 16))  # text glyph = antialiased, unlike Canvas ovals
        dot.pack(side="left", padx=(20, 10), pady=14)
        label = tk.Label(pop, fg=fg, bg=bg, font=(font, 12, "bold"))
        label.pack(side="left", padx=(0, 24), pady=14)
        pop.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(pop.winfo_id())
        dwm = ctypes.windll.dwmapi.DwmSetWindowAttribute
        # ponytail: no-op on Windows 10 (square corners, no border colour), which is fine
        dwm(hwnd, 33, ctypes.byref(ctypes.c_int(2)), 4)       # DWMWA_WINDOW_CORNER_PREFERENCE = ROUND
        dwm(hwnd, 34, ctypes.byref(ctypes.c_uint(border)), 4)  # DWMWA_BORDER_COLOR (COLORREF is BGR; greys are symmetric)
        pop.withdraw()
        hide_id = [None]

        def work_bottom() -> int:
            r = wt.RECT()
            ctypes.windll.user32.SystemParametersInfoW(0x30, 0, ctypes.byref(r), 0)  # SPI_GETWORKAREA: above the taskbar
            return r.bottom or pop.winfo_screenheight()

        def show(text, color, sticky, ms):
            dot.configure(fg=color)
            label.configure(text=text)
            pop.update_idletasks()
            w, h = pop.winfo_reqwidth(), pop.winfo_reqheight()
            pop.geometry(f"+{(pop.winfo_screenwidth() - w) // 2}+{work_bottom() - h - 24}")
            pop.deiconify()
            pop.lift()
            if hide_id[0]:
                root.after_cancel(hide_id[0])
            hide_id[0] = None if sticky else root.after(ms, pop.withdraw)

        def pick(done, box):
            top = tk.Toplevel(root)
            top.attributes("-topmost", True)
            top.geometry("1x1+200+200")
            try:
                box["path"] = filedialog.askopenfilename(
                    parent=top, title="Choose a program",
                    filetypes=[("Programs and shortcuts", "*.exe *.lnk *.bat *.cmd"), ("All files", "*.*")]) or ""
            finally:
                top.destroy()
                done.set()

        def poll():
            try:
                while True:
                    item = self._q.get_nowait()
                    (show if item[0] == "show" else pick)(*item[1:])
            except queue.Empty:
                pass
            root.after(40, poll)

        poll()
        root.mainloop()
