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
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        pop = tk.Toplevel(root)
        pop.overrideredirect(True)
        pop.attributes("-topmost", True)
        pop.attributes("-alpha", 0.93)
        pop.configure(bg="#14161a")
        swatch = tk.Canvas(pop, width=18, height=18, bg="#14161a", highlightthickness=0)
        swatch.pack(side="left", padx=(22, 10), pady=18)
        label = tk.Label(pop, fg="#ffffff", bg="#14161a", font=("Segoe UI", 16, "bold"))
        label.pack(side="left", padx=(0, 26), pady=18)
        pop.withdraw()
        hide_id = [None]

        def show(text, color, sticky, ms):
            swatch.delete("all")
            swatch.create_oval(1, 1, 17, 17, fill=color, outline="")
            label.configure(text=text)
            pop.update_idletasks()
            w, h = pop.winfo_reqwidth(), pop.winfo_reqheight()
            sw, sh = pop.winfo_screenwidth(), pop.winfo_screenheight()
            pop.geometry(f"+{(sw - w) // 2}+{int(sh * 0.72)}")
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
